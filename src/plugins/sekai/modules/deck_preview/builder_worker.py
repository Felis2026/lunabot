from __future__ import annotations

import json
import hashlib
import os
import shutil
import sys
import traceback
import uuid
from pathlib import Path

import zstandard


# ================================ 独立子进程导入路径 ================================ #
# 直接运行本文件可避免 spawn 时重新导入整个 NoneBot 插件树；只把纯 Python
# modules 目录加入路径，builder 不注册任何命令或后台任务。
MODULES_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = Path(__file__).resolve().parents[5]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(MODULES_DIR) not in sys.path:
    sys.path.insert(0, str(MODULES_DIR))

from src.services.deck_recommender.masterdata_spec import (
    get_deck_masterdata_specs,
    get_spec_filenames,
)
from deck_preview.protocol import publish_result
from deck_preview.ruleset import (
    build_and_publish_ruleset,
    run_official_native_canaries,
)


OMAKASE_MUSIC_ID = 10000
OMAKASE_MUSIC_DIFFS = {"master", "expert", "hard"}


def _canonical_json_bytes(data) -> bytes:
    return json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temp_path.open("w", encoding="utf-8", newline="\n") as file:
        json.dump(data, file, ensure_ascii=False, separators=(",", ":"))
        file.flush()
        os.fsync(file.fileno())
    os.replace(temp_path, path)


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temp_path.open("wb") as file:
        file.write(data)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temp_path, path)


def _add_payload_segment(parts: list[bytes], data: bytes) -> None:
    parts.append(len(data).to_bytes(4, "big"))
    parts.append(data)


def _add_omakase_music(musicmetas: list[dict]) -> list[dict]:
    """与正式同步一致，为默认协力组卡补充各难度的平均曲目。"""

    if any(item.get("music_id") == OMAKASE_MUSIC_ID for item in musicmetas):
        return musicmetas
    candidates = [
        item
        for item in musicmetas
        if item.get("difficulty") in OMAKASE_MUSIC_DIFFS
    ]
    if not candidates:
        raise RuntimeError("MusicMetas 中没有可用于生成おまかせ的歌曲")

    omakase = {
        "music_id": OMAKASE_MUSIC_ID,
        "difficulty": None,
        "music_time": 0.0,
        "event_rate": 0.0,
        "base_score": 0.0,
        "base_score_auto": 0.0,
        "skill_score_solo": [0.0 for _ in range(6)],
        "skill_score_auto": [0.0 for _ in range(6)],
        "skill_score_multi": [0.0 for _ in range(6)],
        "fever_score": 0.0,
        "fever_end_time": 0.0,
        "tap_count": 0,
    }
    for item in candidates:
        for key in (
            "music_time",
            "event_rate",
            "base_score",
            "base_score_auto",
            "fever_score",
            "fever_end_time",
            "tap_count",
        ):
            omakase[key] += item[key]
        for key in (
            "skill_score_solo",
            "skill_score_auto",
            "skill_score_multi",
        ):
            for index in range(6):
                omakase[key][index] += item[key][index]

    count = len(candidates)
    for key in (
        "music_time",
        "base_score",
        "base_score_auto",
        "fever_score",
        "fever_end_time",
    ):
        omakase[key] /= count
    omakase["event_rate"] = int(omakase["event_rate"] / count)
    omakase["tap_count"] = int(omakase["tap_count"] / count)
    for key in (
        "skill_score_solo",
        "skill_score_auto",
        "skill_score_multi",
    ):
        omakase[key] = [value / count for value in omakase[key]]

    for difficulty in ("easy", "normal", "hard", "expert", "master", "append"):
        item = dict(omakase)
        item["difficulty"] = difficulty
        musicmetas.append(item)
    return musicmetas


def _build_full_sync_payload(
    result,
    request: dict,
) -> tuple[str, dict]:
    """
    在 builder 子进程内完成大文件读取与 zstd 压缩。

    Bot 主事件循环只读取最终压缩文件并执行异步 HTTP，不参与 JSON 拼接和压缩。
    """

    manifest_path = Path(result.manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    musicmetas_path = Path(request["musicmetas_path"]).resolve()
    raw_musicmetas = json.loads(musicmetas_path.read_text(encoding="utf-8"))
    if not isinstance(raw_musicmetas, list):
        raise RuntimeError("MusicMetas 必须是 JSON 数组")
    musicmetas = _canonical_json_bytes(_add_omakase_music(raw_musicmetas))
    metadata = {
        "protocol_version": 2,
        "instance_role": "preview",
        "data_scope": manifest["data_scope"],
        "region": manifest["native_region"],
        "masterdata_version": (
            f"cn:{manifest['cn_masterdata_version']}"
            f"|jp:{manifest['jp_masterdata_version']}"
        ),
        "masterdata_fingerprint": manifest["scope_fingerprint"],
        "musicmetas_update_ts": int(request["musicmetas_update_ts"]),
        "manifest": manifest,
    }

    payload_parts: list[bytes] = []
    _add_payload_segment(payload_parts, _canonical_json_bytes(metadata))
    masterdata_dir = Path(result.version_dir) / "masterdata"
    for filename, expected_hash in sorted(manifest["files"].items()):
        content = (masterdata_dir / filename).read_bytes()
        actual_hash = "sha256:" + hashlib.sha256(content).hexdigest()
        if actual_hash != expected_hash:
            raise RuntimeError(f"构建后文件哈希变化: {filename}")
        _add_payload_segment(payload_parts, filename.encode("utf-8"))
        _add_payload_segment(payload_parts, content)
    _add_payload_segment(payload_parts, b"musicmetas")
    _add_payload_segment(payload_parts, musicmetas)

    payload_key = hashlib.sha256(
        _canonical_json_bytes(metadata)
        + hashlib.sha256(musicmetas).digest()
    ).hexdigest()
    payload_dir = Path(result.version_dir) / "payloads"
    payload_path = payload_dir / f"{payload_key}.zst"
    if not payload_path.is_file():
        payload_dir.mkdir(parents=True, exist_ok=True)
        temp_path = payload_path.with_name(
            f".{payload_path.name}.{os.getpid()}.tmp"
        )
        compressed = zstandard.ZstdCompressor().compress(b"".join(payload_parts))
        with temp_path.open("wb") as file:
            file.write(compressed)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_path, payload_path)
    return str(payload_path), metadata


# ================================ 正式CN切回探针 ================================ #

def _calc_source_stat_fingerprint(paths: dict[str, str]) -> str:
    """复现 Bot 正式同步所用的文件指纹，确保探针与已确认节点针对同一批 CN 文件。"""

    digest = hashlib.md5()
    for path_text in paths.values():
        path = Path(path_text)
        stat = path.stat()
        digest.update(
            f"{path.name}:{stat.st_size}:{stat.st_mtime_ns}\n".encode("utf-8")
        )
    return digest.hexdigest()


def _verify_request_source_fingerprints(request: dict) -> None:
    """拒绝把 provider 取样之后已经变化的 CN/JP 文件发布成旧身份。"""

    for region in ("cn", "jp"):
        expected = str(request[f"{region}_source_fingerprint"])
        actual = _calc_source_stat_fingerprint(request[f"{region}_paths"])
        if actual != expected:
            raise RuntimeError(
                f"{region.upper()} 来源指纹已变化，等待下一轮重新构建"
            )


def _run_official_canaries(
    request: dict,
    expected_filenames: tuple[str, ...],
) -> dict:
    """
    复制一份只含 CN 的稳定快照并执行原生计算。

    该结果只负责证明正式切回候选；任何失败均收敛为“未确认”，不会妨碍
    已通过自身完整 canary 的 preview 规则发布。
    """

    candidates = request.get("official_canary_candidates", [])
    if not candidates:
        return {
            "source_fingerprint": None,
            "confirmed_event_ids": [],
            "failures": {},
            "cold_load": False,
        }

    expected_fingerprint = str(
        request.get("official_canary_fingerprint", "")
    )
    cn_paths = request["cn_paths"]
    if set(cn_paths) != set(expected_filenames):
        return {
            "source_fingerprint": expected_fingerprint or None,
            "confirmed_event_ids": [],
            "failures": {
                "_source": "CN 文件集合与共享 MasterData 规格不一致",
            },
            "cold_load": False,
        }

    # ================================ CN稳定快照 ================================ #
    output_root = Path(request["output_root"]).resolve()
    canary_root = output_root / "official_canary"
    job_root = canary_root / uuid.uuid4().hex
    masterdata_dir = job_root / "masterdata"
    try:
        before_fingerprint = _calc_source_stat_fingerprint(cn_paths)
        if before_fingerprint != expected_fingerprint:
            return {
                "source_fingerprint": before_fingerprint,
                "confirmed_event_ids": [],
                "failures": {
                    "_source": "CN 来源指纹已变化，等待下一轮正式同步",
                },
                "cold_load": False,
            }

        for filename in expected_filenames:
            _atomic_write_bytes(
                masterdata_dir / filename,
                Path(cn_paths[filename]).read_bytes(),
            )
        after_copy_fingerprint = _calc_source_stat_fingerprint(cn_paths)
        if after_copy_fingerprint != expected_fingerprint:
            return {
                "source_fingerprint": after_copy_fingerprint,
                "confirmed_event_ids": [],
                "failures": {
                    "_source": "复制 CN 快照期间来源发生变化，等待下一轮",
                },
                "cold_load": False,
            }

        # ================================ 原生计算与二次复核 ================================ #
        result = run_official_native_canaries(
            masterdata_dir,
            request["musicmetas_path"],
            candidates,
        )
        final_fingerprint = _calc_source_stat_fingerprint(cn_paths)
        if final_fingerprint != expected_fingerprint:
            return {
                "source_fingerprint": final_fingerprint,
                "confirmed_event_ids": [],
                "failures": {
                    "_source": "执行正式探针期间 CN 来源发生变化，结果作废",
                },
                "cold_load": True,
            }
        return {
            **result,
            "source_fingerprint": expected_fingerprint,
        }
    except Exception as exc:
        return {
            "source_fingerprint": expected_fingerprint or None,
            "confirmed_event_ids": [],
            "failures": {
                "_source": f"{type(exc).__name__}: {exc}",
            },
            "cold_load": False,
        }
    finally:
        resolved_job_root = job_root.resolve()
        resolved_canary_root = canary_root.resolve()
        if (
            resolved_job_root != resolved_canary_root
            and resolved_canary_root in resolved_job_root.parents
            and resolved_job_root.exists()
        ):
            shutil.rmtree(resolved_job_root)


def _invalidate_stale_official_canary(
    request: dict,
    result: dict,
) -> dict:
    """preview 构建失败后再次确认 CN 未变化，避免返回已经过期的正式探针。"""

    actual = _calc_source_stat_fingerprint(request["cn_paths"])
    expected = str(request["official_canary_fingerprint"])
    if actual == expected:
        return result
    return {
        "source_fingerprint": actual,
        "confirmed_event_ids": [],
        "failures": {
            "_source": "preview 构建期间 CN 来源发生变化，正式探针结果作废",
        },
        "cold_load": bool(result.get("cold_load")),
    }


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: builder_worker.py <request.json> <result.json>", file=sys.stderr)
        return 2

    request_path = Path(sys.argv[1]).resolve()
    result_path = Path(sys.argv[2]).resolve()
    try:
        request = json.loads(request_path.read_text(encoding="utf-8"))
        specs = get_deck_masterdata_specs(
            include_mysekai=bool(request["include_mysekai"]),
            include_wl_limited_bonus=bool(request["include_wl_limited_bonus"]),
        )
        expected_filenames = get_spec_filenames(specs)
        _verify_request_source_fingerprints(request)
        official_canary = _run_official_canaries(
            request,
            expected_filenames,
        )
        try:
            result = build_and_publish_ruleset(
                cn_paths=request["cn_paths"],
                jp_paths=request["jp_paths"],
                output_root=request["output_root"],
                cn_masterdata_version=request["cn_masterdata_version"],
                jp_masterdata_version=request["jp_masterdata_version"],
                expected_filenames=expected_filenames,
                musicmetas_path=request["musicmetas_path"],
                allowed_event_types=request.get(
                    "allowed_event_types",
                    ["marathon", "world_bloom"],
                ),
                event_allowlist=request.get("event_allowlist", []),
                run_canaries=bool(request.get("run_canaries", True)),
            )
        except Exception as exc:
            # 正式切回不能被无关的 JP preview 构建故障拖住；调度器会先消费
            # 已确认的纯 CN 探针，再把 preview 状态标为 ERROR。
            official_canary = _invalidate_stale_official_canary(
                request,
                official_canary,
            )
            _atomic_write_json(result_path, {
                "ok": True,
                "result": {
                    "official_canary": official_canary,
                    "preview_error": {
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                        "traceback": traceback.format_exc(limit=20),
                    },
                },
            })
            return 0
        _verify_request_source_fingerprints(request)
        sync_payload_path, sync_metadata = _build_full_sync_payload(result, request)
        result_data = result.to_dict()
        result_data["sync_payload_path"] = sync_payload_path
        result_data["sync_metadata"] = sync_metadata
        result_data["official_canary"] = official_canary
        # payload、manifest 和 canary 都完成后才让非 owner 节点看见这一版本。
        publish_result(request["output_root"], request, result_data)
        _atomic_write_json(result_path, {"ok": True, "result": result_data})
        return 0
    except BaseException as exc:
        _atomic_write_json(result_path, {
            "ok": False,
            "error_type": type(exc).__name__,
            "error": str(exc),
            "traceback": traceback.format_exc(limit=20),
        })
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
