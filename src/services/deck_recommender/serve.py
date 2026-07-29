from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
import uvicorn
from sekai_deck_recommend_cpp import SekaiDeckRecommend

from utils import *
from worker import *
from config import *
from masterdata_spec import (
    get_native_package_build_id,
    get_preview_expected_filenames,
    get_preview_manifest_fingerprint_input,
)

try:
    import uvloop

    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
except ImportError:
    pass


# ================================ 服务运行状态 ================================ #

DATA_OPERATION_LOCK = asyncio.Lock()
SERVICE_STATE: dict[str, Any] = {
    "maintenance": False,
    "data_healthy": True,
    "started_at": int(time.time()),
    "worker_fingerprints": {},
    "last_error": "",
}


def _sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _canonical_json_bytes(data: Any) -> bytes:
    return json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _native_package_version() -> str:
    try:
        return importlib.metadata.version("sekai-deck-recommend-cpp")
    except importlib.metadata.PackageNotFoundError:
        return "missing"


def _safe_segment_name(name: str) -> bool:
    """同步协议只接受单层 JSON 文件名，拒绝路径穿越与未知扩展名。"""

    return (
        bool(name)
        and name == os.path.basename(name)
        and "/" not in name
        and "\\" not in name
        and name not in {".", ".."}
        and name.endswith(".json")
    )


def _atomic_write_json(path: str, data: dict[str, Any]) -> None:
    """写临时文件、刷盘后原子替换，供 active 指针等路由状态使用。"""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    with temp_path.open("wb") as file:
        file.write(orjson.dumps(data, option=orjson.OPT_INDENT_2))
        file.flush()
        os.fsync(file.fileno())
    os.replace(temp_path, target)


def _read_db() -> dict[str, Any]:
    return load_json(DB_PATH, default={})


def _stored_status(region: str) -> dict[str, Any]:
    db = _read_db()
    active_fingerprint = None
    if Path(ACTIVE_POINTER_PATH).is_file():
        try:
            pointer = load_json(ACTIVE_POINTER_PATH)
            if pointer.get("native_region") == region:
                active_fingerprint = pointer.get("scope_fingerprint")
        except Exception:
            # 状态接口必须保持可读；健康状态会单独标明指针无法解析。
            pass
    return {
        "masterdata_version": db.get("masterdata_version", {}).get(region),
        "stored_fingerprint": db.get("masterdata_fingerprint", {}).get(region),
        "active_fingerprint": active_fingerprint,
        "musicmetas_update_ts": db.get("musicmetas_update_ts", {}).get(region),
        "loaded_fingerprints": SERVICE_STATE["worker_fingerprints"].get(region, {}),
    }


def _reject_during_maintenance() -> None:
    if SERVICE_STATE["maintenance"]:
        raise HTTPException(
            status_code=503,
            detail="组卡服务正在切换数据版本，请稍后再试",
        )
    if not SERVICE_STATE["data_healthy"]:
        raise HTTPException(
            status_code=503,
            detail="组卡服务数据回滚未完整确认，已停止接收计算请求",
        )


# ================================ v1兼容同步 ================================ #

def update_data_v1(
    region: str,
    masterdata_version: str,
    masterdata_fingerprint: str | None,
    masterdata: dict[str, bytes] | None,
    musicmetas_update_ts: int,
    musicmetas: bytes | None,
) -> tuple[dict[str, Any], bool]:
    """保留旧客户端逐文件同步语义，并返回本次是否改变了磁盘状态。"""

    db = _read_db()
    missing_data: set[str] = set()
    changed = False

    # ================================ MasterData版本与指纹校验 ================================ #
    current_masterdata_version = db.get("masterdata_version", {}).get(region)
    current_masterdata_fingerprint = db.get(
        "masterdata_fingerprint",
        {},
    ).get(region)
    masterdata_changed = current_masterdata_version != masterdata_version
    if (
        masterdata_fingerprint is not None
        and current_masterdata_fingerprint != masterdata_fingerprint
    ):
        masterdata_changed = True

    if masterdata_changed:
        if not masterdata:
            missing_data.add("masterdata")
        else:
            local_md_dir = pjoin(DATA_DIR, "masterdata", region)
            for name, md in masterdata.items():
                if not _safe_segment_name(name):
                    raise HTTPException(status_code=400, detail=f"非法文件名: {name}")
                write_file(pjoin(local_md_dir, name), md)
            db.setdefault("masterdata_version", {})[region] = masterdata_version
            db.setdefault("masterdata_paths", {})[region] = local_md_dir
            if masterdata_fingerprint is not None:
                db.setdefault("masterdata_fingerprint", {})[
                    region
                ] = masterdata_fingerprint
            changed = True
            log(
                f"更新 {region} MasterData "
                f"version={current_masterdata_version} -> {masterdata_version} "
                f"fingerprint={current_masterdata_fingerprint or 'None'} "
                f"-> {masterdata_fingerprint or 'None'}"
            )

    current_musicmetas_update_ts = db.get("musicmetas_update_ts", {}).get(region)
    if current_musicmetas_update_ts != musicmetas_update_ts:
        if not musicmetas:
            missing_data.add("musicmetas")
        else:
            local_mm_path = pjoin(DATA_DIR, f"musicmetas_{region}.json")
            write_file(local_mm_path, musicmetas)
            db.setdefault("musicmetas_update_ts", {})[
                region
            ] = musicmetas_update_ts
            db.setdefault("musicmetas_paths", {})[region] = local_mm_path
            changed = True
            current_ts_text = (
                datetime.fromtimestamp(current_musicmetas_update_ts).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                if current_musicmetas_update_ts
                else "None"
            )
            local_ts_text = datetime.fromtimestamp(
                musicmetas_update_ts
            ).strftime("%Y-%m-%d %H:%M:%S")
            log(f"更新 {region} MusicMetas {current_ts_text} -> {local_ts_text}")

    dump_json(db, DB_PATH)
    if missing_data:
        log(f"{region} 检测到数据更新不完整，缺少：{', '.join(missing_data)}")
        raise HTTPException(
            status_code=426,
            detail={
                "missing_data": sorted(missing_data),
                "message": "缺少必要的数据，请上传完整数据",
            },
        )
    return db, changed


# 兼容少量直接导入旧函数名的部署脚本。
update_data = update_data_v1


# ================================ v2整包校验与发布 ================================ #

def _validate_v2_metadata(data: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    if data.get("protocol_version") != 2:
        raise HTTPException(status_code=400, detail="未知同步协议版本")
    if data.get("instance_role") != INSTANCE_ROLE:
        raise HTTPException(status_code=409, detail="同步目标实例角色不匹配")
    if data.get("data_scope") != DATA_SCOPE:
        raise HTTPException(status_code=409, detail="同步 data_scope 不匹配")

    region = data.get("region")
    if not isinstance(region, str) or not region:
        raise HTTPException(status_code=400, detail="缺少同步区服")
    if INSTANCE_ROLE == "preview" and region != NATIVE_REGION:
        raise HTTPException(status_code=409, detail="preview 只接受配置的原生区服")

    expected_fingerprint = data.get("masterdata_fingerprint")
    manifest = data.get("manifest")
    if not isinstance(manifest, dict):
        raise HTTPException(status_code=400, detail="v2 同步缺少 manifest")
    if manifest.get("data_scope") != DATA_SCOPE:
        raise HTTPException(status_code=409, detail="manifest data_scope 不匹配")
    if manifest.get("native_region") != region:
        raise HTTPException(status_code=409, detail="manifest native_region 不匹配")
    if manifest.get("scope_fingerprint") != expected_fingerprint:
        raise HTTPException(status_code=409, detail="manifest 指纹与请求不一致")
    if manifest.get("native_package_version") != _native_package_version():
        raise HTTPException(status_code=409, detail="原生组卡包版本与 manifest 不一致")
    if (
        manifest.get("native_package_build_id")
        != get_native_package_build_id()
    ):
        raise HTTPException(
            status_code=409,
            detail="原生组卡包构建身份与 manifest 不一致",
        )

    expected_version = (
        f"cn:{manifest.get('cn_masterdata_version')}"
        f"|jp:{manifest.get('jp_masterdata_version')}"
    )
    if data.get("masterdata_version") != expected_version:
        raise HTTPException(status_code=409, detail="MasterData 版本链不一致")
    try:
        musicmetas_update_ts = int(data["musicmetas_update_ts"])
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=400,
            detail="musicmetas_update_ts 必须是整数",
        ) from exc
    if musicmetas_update_ts <= 0:
        raise HTTPException(
            status_code=400,
            detail="musicmetas_update_ts 必须大于 0",
        )

    fingerprint_input = get_preview_manifest_fingerprint_input(manifest)
    actual_fingerprint = _sha256_bytes(_canonical_json_bytes(fingerprint_input))
    if actual_fingerprint != expected_fingerprint:
        raise HTTPException(status_code=409, detail="manifest 内容指纹复算失败")
    return region, expected_fingerprint, manifest


def _validate_v2_masterdata(
    manifest: dict[str, Any],
    masterdata: dict[str, bytes],
) -> None:
    file_hashes = manifest.get("files")
    if not isinstance(file_hashes, dict):
        raise HTTPException(status_code=400, detail="manifest.files 格式错误")
    expected_files = set(file_hashes)
    if INSTANCE_ROLE == "preview":
        shared_files = set(get_preview_expected_filenames())
        if expected_files != shared_files:
            raise HTTPException(
                status_code=409,
                detail="manifest 文件集合与 preview 共享规格不一致",
            )
    if set(masterdata) != expected_files:
        raise HTTPException(
            status_code=400,
            detail={
                "message": "v2 MasterData 必须上传完整精确文件集",
                "missing_files": sorted(expected_files - set(masterdata)),
                "extra_files": sorted(set(masterdata) - expected_files),
            },
        )

    for name, content in masterdata.items():
        if not _safe_segment_name(name):
            raise HTTPException(status_code=400, detail=f"非法文件名: {name}")
        if _sha256_bytes(content) != file_hashes.get(name):
            raise HTTPException(status_code=409, detail=f"{name} SHA-256 不一致")
        try:
            parsed = loads_json(content)
        except Exception as exc:
            raise HTTPException(
                status_code=400,
                detail=f"{name} 不是合法 JSON: {get_exc_desc(exc)}",
            ) from exc
        if not isinstance(parsed, list):
            raise HTTPException(status_code=400, detail=f"{name} 必须是 JSON 数组")


def _validate_existing_version(
    version_dir: Path,
    manifest: dict[str, Any],
) -> bool:
    masterdata_dir = version_dir / "masterdata" / manifest["native_region"]
    if not _validate_masterdata_directory(masterdata_dir, manifest):
        return False
    manifest_path = version_dir / "manifest.json"
    if not manifest_path.is_file():
        return False
    try:
        stored_manifest = loads_json(manifest_path.read_bytes())
    except Exception:
        return False
    return stored_manifest == manifest


def _validate_masterdata_directory(
    masterdata_dir: Path,
    manifest: dict[str, Any],
) -> bool:
    """复核当前可加载目录的精确文件集和内容，禁止同指纹磁盘损坏被握手误认。"""

    if not masterdata_dir.is_dir():
        return False
    actual_files = {
        path.name
        for path in masterdata_dir.iterdir()
        if path.is_file()
    }
    if actual_files != set(manifest["files"]):
        return False
    for name, expected_hash in manifest["files"].items():
        path = masterdata_dir / name
        if not path.is_file() or _sha256_bytes(path.read_bytes()) != expected_hash:
            return False
    return True


def _prepare_v2_publication(
    data: dict[str, Any],
    region: str,
    expected_fingerprint: str,
    manifest: dict[str, Any],
    masterdata: dict[str, bytes],
    musicmetas: bytes | None,
) -> tuple[dict[str, Any], Path | None]:
    """
    把新数据写到不可路由的内容寻址目录，并用独立原生实例冷加载。

    此函数不修改 DB/active 指针；只有所有 worker 确认后才由异步发布阶段激活。
    """

    db = _read_db()
    new_db = deepcopy(db)
    current_fingerprint = db.get("masterdata_fingerprint", {}).get(region)
    current_music_ts = db.get("musicmetas_update_ts", {}).get(region)
    masterdata_changed = current_fingerprint != expected_fingerprint
    musicmetas_update_ts = int(data["musicmetas_update_ts"])
    musicmetas_changed = current_music_ts != musicmetas_update_ts

    versions_root = Path(DATA_DIR).resolve() / "versions"
    staging_root = Path(DATA_DIR).resolve() / "staging"
    staging_dir: Path | None = None
    published_version_dir: Path | None = None
    target_md_dir = Path(
        db.get("masterdata_paths", {}).get(
            region,
            pjoin(DATA_DIR, "masterdata", region),
        )
    ).resolve()
    target_mm_path = Path(
        db.get("musicmetas_paths", {}).get(
            region,
            pjoin(DATA_DIR, f"musicmetas_{region}.json"),
        )
    ).resolve()

    # DB 指纹相同仍要复核磁盘内容；否则损坏目录会在 metadata-only 握手中
    # 被错误确认，worker 也只会回报 DB 中的旧指纹。
    if (
        not masterdata_changed
        and not _validate_masterdata_directory(target_md_dir, manifest)
    ):
        masterdata_changed = True

    missing_data: list[str] = []
    if masterdata_changed and not masterdata:
        missing_data.append("masterdata")
    if musicmetas_changed and not musicmetas:
        missing_data.append("musicmetas")
    if missing_data:
        raise HTTPException(
            status_code=426,
            detail={
                "missing_data": missing_data,
                "message": "缺少必要的数据，请上传完整数据",
            },
        )

    try:
        # 即使目标指纹未变化，也不能接受带有多余、缺失或哈希不符文件的 v2 包。
        if masterdata:
            _validate_v2_masterdata(manifest, masterdata)
        if masterdata_changed:
            token = expected_fingerprint.removeprefix("sha256:")
            if (
                len(token) != 64
                or any(character not in "0123456789abcdef" for character in token)
            ):
                raise HTTPException(status_code=400, detail="非法 scope fingerprint")

            staging_dir = staging_root / uuid.uuid4().hex
            staging_md_dir = staging_dir / "masterdata" / region
            for name, content in masterdata.items():
                write_file(str(staging_md_dir / name), content)
            dump_json(manifest, str(staging_dir / "manifest.json"))

            published_version_dir = versions_root / token
            if published_version_dir.exists():
                if not _validate_existing_version(published_version_dir, manifest):
                    raise HTTPException(
                        status_code=409,
                        detail="同指纹版本目录已存在但内容损坏，拒绝覆盖",
                    )
            else:
                versions_root.mkdir(parents=True, exist_ok=True)
                os.replace(staging_dir, published_version_dir)
                staging_dir = None

            target_md_dir = published_version_dir / "masterdata" / region
            new_db.setdefault("masterdata_version", {})[region] = str(
                data["masterdata_version"]
            )
            new_db.setdefault("masterdata_fingerprint", {})[
                region
            ] = expected_fingerprint
            new_db.setdefault("masterdata_paths", {})[region] = str(target_md_dir)
            new_db.setdefault("masterdata_manifests", {})[region] = manifest

        if musicmetas_changed:
            music_hash = _sha256_bytes(musicmetas).removeprefix("sha256:")
            music_dir = Path(DATA_DIR).resolve() / "musicmetas_versions"
            target_mm_path = music_dir / f"{region}-{musicmetas_update_ts}-{music_hash}.json"
            if not target_mm_path.is_file():
                write_file(str(target_mm_path), musicmetas)
            new_db.setdefault("musicmetas_update_ts", {})[
                region
            ] = musicmetas_update_ts
            new_db.setdefault("musicmetas_paths", {})[region] = str(target_mm_path)

        if not target_md_dir.is_dir() or not target_mm_path.is_file():
            missing: list[str] = []
            if not target_md_dir.is_dir():
                missing.append("masterdata")
            if not target_mm_path.is_file():
                missing.append("musicmetas")
            raise HTTPException(
                status_code=426,
                detail={
                    "missing_data": missing,
                    "message": "服务端尚无可加载的完整数据",
                },
            )

        # 冷加载在主进程临时实例中完成，失败时 DB 与 worker 均保持旧版本。
        probe = SekaiDeckRecommend()
        probe.update_masterdata(str(target_md_dir), region)
        probe.update_musicmetas(str(target_mm_path), region)
        return new_db, published_version_dir
    finally:
        if staging_dir is not None and staging_dir.exists():
            resolved = staging_dir.resolve()
            if staging_root.resolve() not in resolved.parents:
                raise RuntimeError("拒绝清理 staging 根目录之外的路径")
            shutil.rmtree(resolved)


async def _ensure_workers_loaded(
    region: str,
    expected_fingerprint: str | None,
) -> dict[str, str | None]:
    """在维护窗口中独占全部 worker，并收集每个进程的加载确认。"""

    async with WorkerContext.reserve_all_workers(task_timeout=180) as contexts:
        results = await asyncio.gather(*[
            ctx.ensure_data_loaded(region, expected_fingerprint)
            for ctx in contexts
        ])
    errors = [
        result.get("message", "内部错误")
        for result in results
        if result.get("status") != "success"
    ]
    if errors:
        raise RuntimeError("；".join(errors))
    loaded = {
        str(result["worker_id"]): result.get("loaded_fingerprint")
        for result in results
    }
    if (
        len(loaded) != WorkerContext.worker_num
        or set(loaded.values()) != {expected_fingerprint}
    ):
        raise RuntimeError("未收到全部 worker 的目标数据指纹确认")
    SERVICE_STATE["worker_fingerprints"][region] = loaded
    return loaded


def _write_active_pointer(
    region: str,
    expected_fingerprint: str,
    version_dir: Path | None,
) -> None:
    if INSTANCE_ROLE != "preview":
        return
    if version_dir is None:
        db = _read_db()
        md_path = db.get("masterdata_paths", {}).get(region)
        if not md_path:
            raise RuntimeError("无法从 DB 恢复 preview 激活目录")
        version_dir = Path(md_path).resolve().parents[1]
    versions_root = (Path(DATA_DIR).resolve() / "versions").resolve()
    version_dir = version_dir.resolve()
    if version_dir == versions_root or versions_root not in version_dir.parents:
        raise RuntimeError("拒绝把 preview active 指针写到版本目录之外")
    pointer = {
        "instance_role": INSTANCE_ROLE,
        "data_scope": DATA_SCOPE,
        "native_region": region,
        "scope_fingerprint": expected_fingerprint,
        "version_dir": str(version_dir),
        "activated_at": int(time.time()),
    }
    _atomic_write_json(ACTIVE_POINTER_PATH, pointer)


async def _handle_v2_update(
    data: dict[str, Any],
    masterdata: dict[str, bytes],
    musicmetas: bytes | None,
) -> dict[str, Any]:
    region, expected_fingerprint, manifest = _validate_v2_metadata(data)
    old_db = _read_db()
    old_pointer = (
        Path(ACTIVE_POINTER_PATH).read_bytes()
        if Path(ACTIVE_POINTER_PATH).is_file()
        else None
    )
    new_db, version_dir = await asyncio.to_thread(
        _prepare_v2_publication,
        data,
        region,
        expected_fingerprint,
        manifest,
        masterdata,
        musicmetas,
    )

    dump_json(new_db, DB_PATH)
    try:
        loaded = await _ensure_workers_loaded(region, expected_fingerprint)
        _write_active_pointer(region, expected_fingerprint, version_dir)
        SERVICE_STATE["data_healthy"] = True
        SERVICE_STATE["last_error"] = ""
    except BaseException:
        # ================================ 激活失败回滚 ================================ #
        # 内容寻址版本可以保留为未激活缓存，但 DB、worker 与 active 指针必须一起回旧版。
        rollback_errors: list[str] = []
        try:
            dump_json(old_db, DB_PATH)
        except Exception as rollback_exc:
            rollback_errors.append(
                f"恢复DB失败: {get_exc_desc(rollback_exc)}"
            )
        old_fingerprint = old_db.get("masterdata_fingerprint", {}).get(region)
        SERVICE_STATE["worker_fingerprints"][region] = {}
        if old_fingerprint:
            try:
                await _ensure_workers_loaded(region, old_fingerprint)
            except Exception as rollback_exc:
                rollback_errors.append(
                    f"恢复worker失败: {get_exc_desc(rollback_exc)}"
                )
        else:
            # 首次发布没有旧版本可重新加载；部分 worker 可能已切到新数据，
            # 因此必须失败关闭，等待下一次完整 v2 发布重新建立一致状态。
            rollback_errors.append("首次发布失败且没有可回滚的旧worker指纹")
        try:
            if old_pointer is not None:
                write_file(ACTIVE_POINTER_PATH, old_pointer)
            else:
                remove_file(ACTIVE_POINTER_PATH)
        except Exception as rollback_exc:
            rollback_errors.append(
                f"恢复active指针失败: {get_exc_desc(rollback_exc)}"
            )
        if rollback_errors:
            SERVICE_STATE["data_healthy"] = False
            SERVICE_STATE["last_error"] = "；".join(rollback_errors)
            error("组卡数据回滚未完整确认:", SERVICE_STATE["last_error"])
        else:
            SERVICE_STATE["data_healthy"] = True
            SERVICE_STATE["last_error"] = ""
        raise

    return {
        "protocol_version": 2,
        "instance_role": INSTANCE_ROLE,
        "data_scope": DATA_SCOPE,
        "region": region,
        "stored_fingerprint": expected_fingerprint,
        "loaded_fingerprint": expected_fingerprint,
        "worker_fingerprints": loaded,
        "worker_num": len(loaded),
        "data_healthy": True,
    }


# ================================ 请求负载解析 ================================ #

async def extract_decompressed_payload(request: Request) -> list[bytes]:
    try:
        payload = decompress_zstd(await request.body())
    except Exception as exc:
        raise HTTPException(status_code=400, detail="请求体不是合法 zstd 数据") from exc
    segments = []
    index = 0
    while index < len(payload):
        if index + 4 > len(payload):
            raise HTTPException(status_code=400, detail="数据格式错误")
        segment_size = int.from_bytes(payload[index:index + 4], "big")
        index += 4
        if index + segment_size > len(payload):
            raise HTTPException(status_code=400, detail="数据格式错误")
        segment = payload[index:index + segment_size]
        segments.append(segment)
        index += segment_size
    return segments


def _parse_update_segments(
    segments: list[bytes],
) -> tuple[dict[str, Any], dict[str, bytes], bytes | None]:
    if not segments or (len(segments) - 1) % 2 != 0:
        raise HTTPException(status_code=400, detail="更新数据分段数量错误")
    data = loads_json(segments[0])
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="更新元数据格式错误")

    masterdata: dict[str, bytes] = {}
    musicmetas: bytes | None = None
    seen_keys: set[str] = set()
    for index in range(1, len(segments), 2):
        try:
            key = segments[index].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HTTPException(status_code=400, detail="更新文件名不是 UTF-8") from exc
        if key in seen_keys:
            raise HTTPException(status_code=400, detail=f"重复数据段: {key}")
        seen_keys.add(key)
        value = segments[index + 1]
        if key == "musicmetas":
            musicmetas = value
        else:
            masterdata[key] = value
    return data, masterdata, musicmetas


# ================================ API ================================ #

app = FastAPI()


@app.get("/health")
async def health():
    processes = list(WorkerContext.all_processes.values())
    workers_alive = (
        len(processes) == WorkerContext.worker_num
        and WorkerContext.worker_num > 0
        and all(process.is_alive() for process in processes)
    )
    healthy = workers_alive and SERVICE_STATE["data_healthy"]
    return {
        "status": "ok" if healthy else "degraded",
        "instance_role": INSTANCE_ROLE,
        "data_scope": DATA_SCOPE,
        "maintenance": SERVICE_STATE["maintenance"],
        "data_healthy": SERVICE_STATE["data_healthy"],
        "last_error": SERVICE_STATE["last_error"],
        "worker_num": WorkerContext.worker_num,
        "workers_alive": workers_alive,
        "native_package_version": _native_package_version(),
        "native_package_build_id": get_native_package_build_id(),
    }


@app.get("/data_status")
async def data_status(region: str | None = None):
    target_region = region or NATIVE_REGION or "cn"
    return {
        "instance_role": INSTANCE_ROLE,
        "data_scope": DATA_SCOPE,
        "native_region": NATIVE_REGION or None,
        "maintenance": SERVICE_STATE["maintenance"],
        "data_healthy": SERVICE_STATE["data_healthy"],
        "last_error": SERVICE_STATE["last_error"],
        "worker_num": WorkerContext.worker_num,
        "native_package_version": _native_package_version(),
        "native_package_build_id": get_native_package_build_id(),
        "region": target_region,
        **_stored_status(target_region),
    }


@app.post("/update_data")
async def update_endpoint(request: Request):
    async with DATA_OPERATION_LOCK:
        SERVICE_STATE["maintenance"] = True
        try:
            segments = await extract_decompressed_payload(request)
            data, masterdata, musicmetas = _parse_update_segments(segments)
            if data.get("protocol_version", 1) == 2:
                return await _handle_v2_update(data, masterdata, musicmetas)

            region = data["region"]
            db, changed = update_data_v1(
                region,
                data["masterdata_version"],
                data.get("masterdata_fingerprint"),
                masterdata,
                data["musicmetas_update_ts"],
                musicmetas,
            )
            expected_fingerprint = db.get("masterdata_fingerprint", {}).get(region)
            loaded = SERVICE_STATE["worker_fingerprints"].get(region, {})
            if changed or set(loaded.values()) != {expected_fingerprint}:
                loaded = await _ensure_workers_loaded(region, expected_fingerprint)
            SERVICE_STATE["data_healthy"] = True
            SERVICE_STATE["last_error"] = ""
            return {
                "protocol_version": 1,
                "region": region,
                "stored_fingerprint": expected_fingerprint,
                "loaded_fingerprint": expected_fingerprint,
                "worker_fingerprints": loaded,
                "worker_num": len(loaded),
                "data_healthy": True,
            }
        except HTTPException:
            raise
        except Exception as exc:
            SERVICE_STATE["last_error"] = get_exc_desc(exc)
            error("更新数据失败")
            raise HTTPException(status_code=500, detail=get_exc_desc(exc)) from exc
        finally:
            SERVICE_STATE["maintenance"] = False


@app.post("/cache_userdata")
async def cache_userdata_endpoint(request: Request):
    async with DATA_OPERATION_LOCK:
        _reject_during_maintenance()
        try:
            segments = await extract_decompressed_payload(request)
            if len(segments) != 1:
                raise HTTPException(status_code=400, detail="用户数据分段数量错误")
            userdata_bytes = segments[0]

            started_at = datetime.now()
            # 缓存写入也使用独占上下文，避免与同一 worker 的推荐响应在共享队列中串包。
            async with WorkerContext.reserve_all_workers(
                task_timeout=60
            ) as contexts:
                all_result = await asyncio.gather(*[
                    ctx.cache_userdata(userdata_bytes)
                    for ctx in contexts
                ])
            elapsed = (datetime.now() - started_at).total_seconds()
            for result in all_result:
                if result["status"] != "success":
                    raise HTTPException(
                        status_code=500,
                        detail=result.get("message", "内部错误"),
                    )

            userdata_hash = all_result[0]["userdata_hash"]
            log(f"缓存用户数据 {userdata_hash} 成功，耗时 {elapsed:.3f} 秒")
            return {"userdata_hash": userdata_hash}
        except HTTPException:
            raise
        except Exception as exc:
            error("缓存用户数据失败")
            raise HTTPException(status_code=500, detail=get_exc_desc(exc)) from exc


@app.post("/recommend")
async def recommend_endpoint(request: Request):
    _reject_during_maintenance()
    try:
        segments = await extract_decompressed_payload(request)
        if len(segments) != 1:
            raise HTTPException(status_code=400, detail="组卡数据分段数量错误")

        data = loads_json(segments[0])
        region = data["region"]
        if INSTANCE_ROLE == "preview" and region != NATIVE_REGION:
            raise HTTPException(status_code=409, detail="preview 原生区服不匹配")
        batch_options = data["batch_options"]
        userdata_hash = data["userdata_hash"]

        async def do_recommend(options):
            start_time = datetime.now()
            async with WorkerContext() as ctx:
                result = await ctx.recommend(region, options, userdata_hash)

            if result["status"] != "success":
                raise HTTPException(
                    status_code=500,
                    detail=result.get("message", "内部错误"),
                )
            SERVICE_STATE["worker_fingerprints"].setdefault(region, {})[
                str(result["worker_id"])
            ] = result.get("loaded_fingerprint")

            total_time = (datetime.now() - start_time).total_seconds()
            wait_time = total_time - result["cost_time"]
            return {
                "result": result["result"],
                "alg": options["algorithm"],
                "cost_time": result["cost_time"],
                "wait_time": wait_time,
            }

        return await asyncio.gather(*[
            do_recommend(options)
            for options in batch_options
        ])
    except HTTPException:
        raise
    except Exception as exc:
        error("组卡请求处理失败")
        raise HTTPException(status_code=500, detail=get_exc_desc(exc)) from exc


if __name__ == "__main__":
    WorkerContext.init_workers(WORKER_NUM)
    log(
        "组卡服务初始化 "
        f"role={INSTANCE_ROLE} scope={DATA_SCOPE} "
        f"worker_num={WORKER_NUM} data_dir={DATA_DIR} "
        f"native={_native_package_version()}+{get_native_package_build_id()}"
    )

    uvicorn.run(
        "serve:app",
        host=HOST,
        port=PORT,
        log_level="warning",
        workers=None,
        timeout_keep_alive=60,
    )
