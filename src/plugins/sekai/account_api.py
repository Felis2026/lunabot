from __future__ import annotations

import copy
import os
import threading
import time

import httpx

from .common import logger


# ================================ 云端账号客户端 ================================ #
# 这里封装的是 Sekai 领域自己的可选云端账号接口，不属于中控台逻辑。
# 如果环境变量未配置，上层业务必须自动回退到本地数据，不能因为云端未启用而整体报错。
DEFAULT_TIMEOUT = 10.0
DEFAULT_CACHE_TTL = 120.0

_cache_lock = threading.Lock()
_state_cache: dict[tuple[str, str], tuple[float, dict]] = {}
_snapshot_cache: dict[str, tuple[float, dict]] = {}
_blacklist_cache: dict[tuple[str, str, str], tuple[float, dict]] = {}


def _get_base_url() -> str | None:
    return (os.getenv("SEKAI_ACCOUNT_API_BASE_URL", "") or "").strip().rstrip("/") or None


def _get_token() -> str | None:
    return (os.getenv("SEKAI_ACCOUNT_API_TOKEN", "") or "").strip() or None


def _get_timeout() -> float:
    raw = (os.getenv("SEKAI_ACCOUNT_API_TIMEOUT", "") or "").strip()
    if not raw:
        return DEFAULT_TIMEOUT
    try:
        return max(3.0, float(raw))
    except ValueError:
        return DEFAULT_TIMEOUT


def _get_cache_ttl() -> float:
    raw = (os.getenv("SEKAI_ACCOUNT_CACHE_TTL", "") or "").strip()
    if not raw:
        return DEFAULT_CACHE_TTL
    try:
        return max(10.0, float(raw))
    except ValueError:
        return DEFAULT_CACHE_TTL


def is_account_cloud_enabled() -> bool:
    return bool(_get_base_url() and _get_token())


def _headers() -> dict[str, str]:
    token = _get_token()
    return {"X-Sekai-Account-Token": token} if token else {}


# ================================ 请求与缓存辅助 ================================ #
# 统一在这一层整理 HTTP 错误与缓存读写，避免上层业务反复处理相同细节。
def _request_json(method: str, path: str, *, params: dict | None = None, json_body: dict | None = None) -> dict:
    base_url = _get_base_url()
    if not base_url:
        raise RuntimeError("cloud account api is not configured")

    url = f"{base_url}{path}"
    try:
        with httpx.Client(timeout=_get_timeout(), verify=False) as client:
            response = client.request(method, url, params=params, json=json_body, headers=_headers())
    except Exception as e:
        raise RuntimeError(f"cloud account api request failed: {e}") from e

    try:
        payload = response.json()
    except Exception:
        payload = {"detail": response.text}

    if response.status_code != 200:
        detail = payload.get("detail") if isinstance(payload, dict) else payload
        raise RuntimeError(f"cloud account api failed: {response.status_code} {detail}")

    if not isinstance(payload, dict):
        raise RuntimeError("cloud account api returned invalid payload")
    return payload


def _get_cached(cache: dict, key):
    with _cache_lock:
        item = cache.get(key)
        if not item:
            return None
        expires_at, value = item
        if expires_at < time.monotonic():
            cache.pop(key, None)
            return None
        return copy.deepcopy(value)


def _set_cached(cache: dict, key, value: dict):
    with _cache_lock:
        cache[key] = (time.monotonic() + _get_cache_ttl(), copy.deepcopy(value))


def invalidate_state_cache(region: str | None = None, qid: str | None = None):
    with _cache_lock:
        if region is None and qid is None:
            _state_cache.clear()
            return
        for key in list(_state_cache.keys()):
            key_region, key_qid = key
            if (region is None or key_region == region) and (qid is None or key_qid == str(qid)):
                _state_cache.pop(key, None)


def invalidate_snapshot_cache():
    with _cache_lock:
        _snapshot_cache.clear()


def invalidate_blacklist_cache():
    with _cache_lock:
        _blacklist_cache.clear()


# ================================ 只读接口 ================================ #
# 单账号状态查询按区服与 qid 做缓存，减少高频指令下对云端的重复请求。
def get_state(region: str, qid: str | int) -> dict:
    qid = str(qid)
    cache_key = (region, qid)
    cached = _get_cached(_state_cache, cache_key)
    if cached is not None:
        return cached
    payload = _request_json("GET", f"/account/sekai/state/{region}/{qid}")
    state = payload.get("state")
    if not isinstance(state, dict):
        raise RuntimeError("cloud account state payload is invalid")
    _set_cached(_state_cache, cache_key, state)
    return state


def get_snapshot(region: str | None = None) -> dict:
    cache_key = region or "*"
    cached = _get_cached(_snapshot_cache, cache_key)
    if cached is not None:
        return cached
    params = {"region": region} if region else None
    payload = _request_json("GET", "/account/sekai/snapshot", params=params)
    snapshot = payload.get("snapshot")
    if not isinstance(snapshot, dict):
        raise RuntimeError("cloud account snapshot payload is invalid")
    _set_cached(_snapshot_cache, cache_key, snapshot)
    return snapshot


def get_blacklist_status(target_type: str, target_value: str | int, *, region: str | None = None) -> dict:
    normalized_value = str(target_value)
    cache_key = (target_type, region or "global", normalized_value)
    cached = _get_cached(_blacklist_cache, cache_key)
    if cached is not None:
        return cached
    params = {
        "target_type": target_type,
        "target_value": normalized_value,
    }
    if region:
        params["region"] = region
    payload = _request_json("GET", "/account/sekai/blacklist/check", params=params)
    status = {
        "active": bool(payload.get("active")),
        "entry": payload.get("entry"),
    }
    _set_cached(_blacklist_cache, cache_key, status)
    return status


# ================================ 写接口 ================================ #
# 所有写操作都必须立即失效相关缓存，否则命令回复会继续显示旧的绑定或黑名单状态。
def add_binding(region: str, qid: str | int, uid: str, *, set_main: bool, operator: str | None = None) -> dict:
    payload = _request_json(
        "POST",
        "/account/sekai/bind",
        json_body={
            "region": region,
            "qid": str(qid),
            "uid": str(uid),
            "set_main": bool(set_main),
            "operator": operator,
        },
    )
    invalidate_state_cache(region, str(qid))
    invalidate_snapshot_cache()
    return payload.get("state", {})


def remove_binding(region: str, qid: str | int, uid: str, *, operator: str | None = None) -> dict:
    payload = _request_json(
        "POST",
        "/account/sekai/unbind",
        json_body={
            "region": region,
            "qid": str(qid),
            "uid": str(uid),
            "operator": operator,
        },
    )
    invalidate_state_cache(region, str(qid))
    invalidate_snapshot_cache()
    return payload.get("state", {})


def set_main_binding(region: str, qid: str | int, uid: str, *, operator: str | None = None) -> dict:
    payload = _request_json(
        "POST",
        "/account/sekai/main-bind",
        json_body={
            "region": region,
            "qid": str(qid),
            "uid": str(uid),
            "operator": operator,
        },
    )
    invalidate_state_cache(region, str(qid))
    invalidate_snapshot_cache()
    return payload.get("state", {})


def swap_bindings(region: str, qid: str | int, uid1: str, uid2: str, *, operator: str | None = None) -> dict:
    payload = _request_json(
        "POST",
        "/account/sekai/swap-bind",
        json_body={
            "region": region,
            "qid": str(qid),
            "uid1": str(uid1),
            "uid2": str(uid2),
            "operator": operator,
        },
    )
    invalidate_state_cache(region, str(qid))
    invalidate_snapshot_cache()
    return payload.get("state", {})


def add_verified_account(region: str, qid: str | int, uid: str, *, operator: str | None = None) -> dict:
    payload = _request_json(
        "POST",
        "/account/sekai/verified/add",
        json_body={
            "region": region,
            "qid": str(qid),
            "uid": str(uid),
            "operator": operator,
        },
    )
    invalidate_state_cache(region, str(qid))
    invalidate_snapshot_cache()
    return payload.get("state", {})


def set_preferences(
    region: str,
    qid: str | int,
    *,
    data_mode: str | None = None,
    suite_source_mode: str | None = None,
    hide_id: bool | None = None,
    hide_suite: bool | None = None,
    operator: str | None = None,
) -> dict:
    payload = _request_json(
        "POST",
        "/account/sekai/preferences",
        json_body={
            "region": region,
            "qid": str(qid),
            "data_mode": data_mode,
            "suite_source_mode": suite_source_mode,
            "hide_id": hide_id,
            "hide_suite": hide_suite,
            "operator": operator,
        },
    )
    invalidate_state_cache(region, str(qid))
    invalidate_snapshot_cache()
    return payload.get("state", {})


def add_blacklist(
    target_type: str,
    target_value: str | int,
    *,
    region: str | None = None,
    reason: str | None = None,
    operator: str | None = None,
) -> dict:
    payload = _request_json(
        "POST",
        "/account/sekai/blacklist/add",
        json_body={
            "target_type": target_type,
            "target_value": str(target_value),
            "region": region,
            "reason": reason,
            "operator": operator,
        },
    )
    invalidate_blacklist_cache()
    invalidate_snapshot_cache()
    return payload.get("entry", {})


def remove_blacklist(
    target_type: str,
    target_value: str | int,
    *,
    region: str | None = None,
    operator: str | None = None,
) -> dict | None:
    payload = _request_json(
        "POST",
        "/account/sekai/blacklist/remove",
        json_body={
            "target_type": target_type,
            "target_value": str(target_value),
            "region": region,
            "operator": operator,
        },
    )
    invalidate_blacklist_cache()
    invalidate_snapshot_cache()
    return payload.get("entry")
