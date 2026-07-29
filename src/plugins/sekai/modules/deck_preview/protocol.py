from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping, Optional

from src.services.deck_recommender.masterdata_spec import (
    get_native_package_build_id,
    get_preview_manifest_fingerprint_input,
)


# ================================ 预演状态与路由模型 ================================ #

PREVIEW_DATA_SCOPE = "cn_jp_preview_v1"
PREVIEW_NATIVE_REGION = "cn"
PUBLISHED_SCHEMA_VERSION = 1


class PreviewStatus(StrEnum):
    """预演规则集从等待来源到可路由的完整状态。"""

    DISABLED = "DISABLED"
    WAITING_FOR_SOURCE = "WAITING_FOR_SOURCE"
    BUILDING = "BUILDING"
    SYNCING = "SYNCING"
    READY = "READY"
    ERROR = "ERROR"


@dataclass(frozen=True)
class PreviewEventResolution:
    """一次命令解析出的规则来源，不修改玩家原始 CN Context。"""

    event_id: int
    event_type: str
    rule_event: dict[str, Any]
    world_bloom_character_id: Optional[int]
    data_scope: str = PREVIEW_DATA_SCOPE
    native_region: str = PREVIEW_NATIVE_REGION
    server_pool: str = "preview"
    is_preview: bool = True
    scope_fingerprint: str = ""
    future_card_ids: frozenset[int] = frozenset()


@dataclass
class PreviewRuntimeState:
    """供命令层读取的轻量状态；完整错误仅写日志，不回显内部路径。"""

    status: PreviewStatus = PreviewStatus.DISABLED
    scope_fingerprint: str = ""
    message: str = ""
    manifest: dict[str, Any] = field(default_factory=dict)
    ready_servers: list[dict[str, Any]] = field(default_factory=list)


class PublishedPreviewError(RuntimeError):
    """已发布 preview 描述符缺失、过期或内容损坏。"""


# ================================ 发布身份 ================================ #

def build_request_identity(request: Mapping[str, Any]) -> dict[str, Any]:
    """提取会改变规则或同步内容的字段，供非 builder 节点拒绝陈旧产物。"""

    candidates = request.get("official_canary_candidates", [])
    return {
        "cn_masterdata_version": str(request["cn_masterdata_version"]),
        "jp_masterdata_version": str(request["jp_masterdata_version"]),
        "cn_source_fingerprint": str(request["cn_source_fingerprint"]),
        "jp_source_fingerprint": str(request["jp_source_fingerprint"]),
        "musicmetas_update_ts": int(request["musicmetas_update_ts"]),
        "native_package_build_id": str(
            request.get(
                "native_package_build_id",
                get_native_package_build_id(),
            )
        ),
        "allowed_event_types": sorted({
            str(value)
            for value in request.get(
                "allowed_event_types",
                ["marathon", "world_bloom"],
            )
        }),
        "route_event_allowlist": sorted({
            int(value)
            for value in request.get(
                "route_event_allowlist",
                request.get("event_allowlist", []),
            )
        }),
        "official_candidate_ids": sorted({
            int(candidate["event_id"])
            for candidate in candidates
        }),
    }


def _atomic_write_json(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temp_path.open("w", encoding="utf-8", newline="\n") as file:
        json.dump(
            data,
            file,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        file.flush()
        os.fsync(file.fileno())
    os.replace(temp_path, path)


def _sha256_file(path: Path) -> str:
    """分块计算 payload 哈希，避免首次发布时把压缩包再次整体读入内存。"""

    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json_bytes(data: Any) -> bytes:
    return json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _relative_path(root: Path, value: str, label: str) -> str:
    path = Path(value).resolve()
    try:
        return path.relative_to(root).as_posix()
    except ValueError as exc:
        raise PublishedPreviewError(
            f"{label} 不在 preview 发布目录内"
        ) from exc


def _resolve_path(root: Path, value: str, label: str) -> Path:
    path = (root / value).resolve()
    if path == root or root not in path.parents:
        raise PublishedPreviewError(f"{label} 指向 preview 发布目录之外")
    return path


# ================================ 原子发布与消费 ================================ #

def publish_result(
    output_root: str,
    request: Mapping[str, Any],
    result: Mapping[str, Any],
) -> None:
    """在 payload 已完整落盘后发布轻量描述符，供只读 Bot 副本消费。"""

    root = Path(output_root).resolve()
    payload_path = Path(str(result["sync_payload_path"])).resolve()
    if not payload_path.is_file():
        raise PublishedPreviewError("准备发布的 preview payload 不存在")
    descriptor_result = dict(result)
    for key in ("version_dir", "manifest_path", "sync_payload_path"):
        descriptor_result[key] = _relative_path(
            root,
            str(result[key]),
            key,
        )

    descriptor = {
        "schema_version": PUBLISHED_SCHEMA_VERSION,
        "request_identity": build_request_identity(request),
        "payload_sha256": _sha256_file(payload_path),
        "result": descriptor_result,
    }
    _atomic_write_json(root / "published.json", descriptor)


def load_published_result(
    output_root: str,
    request: Mapping[str, Any],
) -> dict[str, Any]:
    """校验并读取 builder_owner 已发布的精确版本；不匹配时失败关闭。"""

    root = Path(output_root).resolve()
    descriptor_path = root / "published.json"
    try:
        descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PublishedPreviewError("builder_owner 尚未发布 preview 产物") from exc
    except Exception as exc:
        raise PublishedPreviewError("preview 发布描述符不是合法 JSON") from exc

    if descriptor.get("schema_version") != PUBLISHED_SCHEMA_VERSION:
        raise PublishedPreviewError("preview 发布描述符版本不受支持")
    if descriptor.get("request_identity") != build_request_identity(request):
        raise PublishedPreviewError("已发布 preview 产物与当前来源或策略不一致")

    # ================================ 路径、manifest与payload复核 ================================ #
    raw_result = descriptor.get("result")
    if not isinstance(raw_result, dict):
        raise PublishedPreviewError("preview 发布描述符缺少 result")
    result = dict(raw_result)
    resolved_paths = {
        key: _resolve_path(root, str(result.get(key, "")), key)
        for key in ("version_dir", "manifest_path", "sync_payload_path")
    }
    if not resolved_paths["version_dir"].is_dir():
        raise PublishedPreviewError("已发布 preview 版本目录不存在")
    if (
        resolved_paths["manifest_path"]
        != resolved_paths["version_dir"] / "manifest.json"
        or not resolved_paths["manifest_path"].is_file()
    ):
        raise PublishedPreviewError("已发布 preview manifest 路径非法或不存在")
    if not resolved_paths["sync_payload_path"].is_file():
        raise PublishedPreviewError("已发布 preview payload 不存在")

    manifest = json.loads(
        resolved_paths["manifest_path"].read_text(encoding="utf-8")
    )
    fingerprint = result.get("scope_fingerprint")
    calculated_fingerprint = (
        "sha256:"
        + hashlib.sha256(
            _canonical_json_bytes(
                get_preview_manifest_fingerprint_input(manifest)
            )
        ).hexdigest()
    )
    metadata_manifest = result.get("sync_metadata", {}).get("manifest")
    if (
        manifest.get("scope_fingerprint") != fingerprint
        or calculated_fingerprint != fingerprint
        or metadata_manifest != manifest
        or result.get("sync_metadata", {}).get(
            "masterdata_fingerprint"
        ) != fingerprint
        or result.get("event_ids") != manifest.get("event_ids")
        or result.get("future_card_ids")
        != manifest.get("future_cards", {}).get("ids")
    ):
        raise PublishedPreviewError("已发布 preview 指纹链不一致")
    payload_hash = _sha256_file(resolved_paths["sync_payload_path"])
    if payload_hash != descriptor.get("payload_sha256"):
        raise PublishedPreviewError("已发布 preview payload 哈希不符")

    for key, path in resolved_paths.items():
        result[key] = str(path)
    return result
