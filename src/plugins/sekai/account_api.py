from __future__ import annotations

import asyncio
import copy
import inspect
import os
import threading
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from functools import wraps

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
_cache_generation = 0

# ================================ 异步调用与写入顺序 ================================ #
# 读写请求共用并发名额，限制同步账号请求占用的线程数量。
_request_slots = asyncio.Semaphore(8)
_write_lock = asyncio.Lock()
_write_context = ContextVar("sekai_account_write", default=None)
_read_tasks: dict[tuple, asyncio.Task] = {}


@asynccontextmanager
async def account_write_scope():
    """串行执行本进程的账号写操作，同一任务中的嵌套调用复用写锁。"""
    if _write_context.get() is asyncio.current_task():
        yield
        return
    async with _write_lock:
        token = _write_context.set(asyncio.current_task())
        try:
            yield
        finally:
            _write_context.reset(token)


def serialize_writes(function):
    """覆盖读取、检查和写入的完整过程，避免绑定上限等检查被并发穿透。"""
    @wraps(function)
    async def wrapped(*args, **kwargs):
        async with account_write_scope():
            return await function(*args, **kwargs)
    return wrapped


async def _run_in_thread(function, args, kwargs):
    """在并发名额内将同步账号函数交给线程执行，并返回结果。

    取消后的线程收尾由调用方通过 shield 管理。
    """
    async with _request_slots:
        return await asyncio.to_thread(function, *args, **kwargs)


def _finish_read(key, task):
    """清理已结束的共享读任务，并取出无人等待时的异常。"""
    if _read_tasks.get(key) is task:
        _read_tasks.pop(key, None)
    if not task.cancelled():
        task.exception()


async def call_async(function, *args, **kwargs):
    """在线程中执行同步账号接口，并传播请求异常。

    同一缓存失效代次中的相同读请求共用任务，各调用者获得独立结果副本。
    写请求串行执行。调用者取消时，等待写任务及其缓存失效完成后再释放写锁。
    """
    if function in (get_state, get_snapshot, get_blacklist_status):
        bound = inspect.signature(function).bind(*args, **kwargs)
        bound.apply_defaults()
        arguments = tuple(
            (name, str(value) if name in {'qid', 'target_value'} else value)
            for name, value in bound.arguments.items()
        )
        with _cache_lock:
            generation = _cache_generation
        key = (function, arguments, generation)
        task = _read_tasks.get(key)
        if task is None:
            task = asyncio.create_task(_run_in_thread(function, args, kwargs))
            _read_tasks[key] = task
            task.add_done_callback(lambda finished: _finish_read(key, finished))
        return copy.deepcopy(await asyncio.shield(task))

    async with account_write_scope():
        task = asyncio.create_task(_run_in_thread(function, args, kwargs))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # 写线程可能已被服务端接受，必须等其结束再释放锁，避免后续写入越过它。
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not task.cancelled():
                task.exception()
            raise


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
    """同步请求 Cloud 并返回 JSON 对象，请求或响应异常时抛出 RuntimeError。

    写请求在开始和结束时失效相关账号、黑名单及管理快照缓存。
    """
    base_url = _get_base_url()
    if not base_url:
        raise RuntimeError("cloud account api is not configured")

    url = f"{base_url}{path}"
    def invalidate_write_cache():
        """按当前写请求的路径和目标失效缓存，并推进缓存失效代次。"""
        if method.upper() != "GET":
            if "/blacklist/" in path:
                invalidate_blacklist_cache()
            else:
                body = json_body or {}
                invalidate_state_cache(body.get("region"), body.get("qid"))
            invalidate_snapshot_cache()

    # 请求超时也可能已经完成服务端写入，开始和结束时都失效，旧读不得回填。
    invalidate_write_cache()
    try:
        with httpx.Client(timeout=_get_timeout(), verify=False) as client:
            response = client.request(method, url, params=params, json=json_body, headers=_headers())
    except Exception as e:
        raise RuntimeError(f"cloud account api request failed: {e}") from e
    finally:
        invalidate_write_cache()

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


def _set_cached(cache: dict, key, value: dict, generation: int):
    """仅在失效代次仍匹配时缓存结果副本，丢弃写入前启动的旧读结果。"""
    with _cache_lock:
        if generation == _cache_generation:
            cache[key] = (time.monotonic() + _get_cache_ttl(), copy.deepcopy(value))


def _get_cache_generation() -> int:
    """读取失效代次，用于识别写入前启动的旧读请求。"""
    with _cache_lock:
        return _cache_generation


def invalidate_state_cache(region: str | None = None, qid: str | None = None):
    """失效匹配区服和 QQ 的账号缓存，省略两项时清空全部账号缓存。"""
    global _cache_generation
    with _cache_lock:
        _cache_generation += 1
        if region is None and qid is None:
            _state_cache.clear()
            return
        for key in list(_state_cache.keys()):
            key_region, key_qid = key
            if (region is None or key_region == region) and (qid is None or key_qid == str(qid)):
                _state_cache.pop(key, None)


def invalidate_snapshot_cache():
    """清空管理快照缓存并推进失效代次，阻止旧请求回填。"""
    global _cache_generation
    with _cache_lock:
        _cache_generation += 1
        _snapshot_cache.clear()


def invalidate_blacklist_cache():
    """清空黑名单缓存并推进失效代次，阻止旧请求回填。"""
    global _cache_generation
    with _cache_lock:
        _cache_generation += 1
        _blacklist_cache.clear()


# ================================ 只读接口 ================================ #
# 单账号状态查询按区服与 qid 做缓存，减少高频指令下对云端的重复请求。
def get_state(region: str, qid: str | int) -> dict:
    """同步读取指定区服和 QQ 的账号状态，优先返回有效缓存的独立副本。"""
    qid = str(qid)
    cache_key = (region, qid)
    cached = _get_cached(_state_cache, cache_key)
    if cached is not None:
        return cached
    generation = _get_cache_generation()
    payload = _request_json("GET", f"/account/sekai/state/{region}/{qid}")
    state = payload.get("state")
    if not isinstance(state, dict):
        raise RuntimeError("cloud account state payload is invalid")
    _set_cached(_state_cache, cache_key, state, generation)
    return state


def get_snapshot(region: str | None = None) -> dict:
    """同步读取账号管理快照，region 省略时查询全部区服。"""
    cache_key = region or "*"
    cached = _get_cached(_snapshot_cache, cache_key)
    if cached is not None:
        return cached
    generation = _get_cache_generation()
    params = {"region": region} if region else None
    payload = _request_json("GET", "/account/sekai/snapshot", params=params)
    snapshot = payload.get("snapshot")
    if not isinstance(snapshot, dict):
        raise RuntimeError("cloud account snapshot payload is invalid")
    _set_cached(_snapshot_cache, cache_key, snapshot, generation)
    return snapshot


def get_blacklist_status(target_type: str, target_value: str | int, *, region: str | None = None) -> dict:
    """同步查询 QQ 或游戏 UID 的黑名单状态，返回 active 和 entry 字段。"""
    normalized_value = str(target_value)
    cache_key = (target_type, region or "global", normalized_value)
    cached = _get_cached(_blacklist_cache, cache_key)
    if cached is not None:
        return cached
    generation = _get_cache_generation()
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
    _set_cached(_blacklist_cache, cache_key, status, generation)
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
