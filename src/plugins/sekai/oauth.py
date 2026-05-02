from __future__ import annotations

import asyncio
import os
import secrets
import time
import urllib.parse

from ..utils import *
from .common import config, logger


# ================================ OAuth 配置读取 ================================ #
# 这里专门负责 suite OAuth 的本地辅助逻辑：
# - 读取 Bot 侧公开配置与 .env
# - 生成授权链接
# - 请求云端 OAuth 代理
# - 维护很短的失败冷却
# 只要云端地址或必要配置缺失，就自动回退到“未启用”状态
def _cloud_base() -> str:
    return (os.getenv("SEKAI_ACCOUNT_API_BASE_URL", "") or "").strip().rstrip("/")


def _cloud_token() -> str:
    return (os.getenv("SEKAI_ACCOUNT_API_TOKEN", "") or "").strip()


def _client_id() -> str:
    return config.get("oauth2_client_id", "", raise_exc=False) or ""


def _authorize_url() -> str:
    return config.get("oauth2_authorize_url", "", raise_exc=False) or ""


def _redirect_uri() -> str:
    return config.get("oauth2_redirect_uri", "", raise_exc=False) or ""


def _scope() -> str:
    # `offline_access` 是当前拿到 refresh_token 的必要前提。
    # 这里的默认值也必须带上它，避免用户只配了 client_id / authorize_url
    # 却因为漏填 yaml 里的 scope 而 silently 降级成“1 小时短 token”。
    return config.get(
        "oauth2_scope",
        "user:read bindings:read game-data:read offline_access",
        raise_exc=False,
    ) or "user:read bindings:read game-data:read offline_access"


def _is_oauth_cloud_enabled() -> bool:
    required = [
        _cloud_base(),
        _cloud_token(),
        _client_id(),
        _authorize_url(),
        _redirect_uri(),
    ]
    return all(bool(item) for item in required)


# ================================ Suite 模式归一化 ================================ #
# suite 的新来源模式与 mysekai 的旧 data_mode 现在已经分家：
# - suite 使用 default / oauth / public
# - mysekai 继续沿用旧 data_mode
def _normalize_suite_source_mode(raw: str | None) -> str:
    mode = str(raw or "").strip().lower()
    if mode in {"default", "oauth", "public"}:
        return mode
    if mode in {"latest", "default"}:
        return "default"
    if mode in {"local", "haruki"}:
        return "public"
    return "default"


def _default_suite_source_policy() -> str:
    return _normalize_suite_source_mode(
        config.get("suite_source_policy", "default", raise_exc=False)
    )


def _suite_oauth_fail_cooldown_sec() -> int:
    raw = config.get("suite_oauth_fail_cooldown_sec", 60, raise_exc=False)
    try:
        return max(0, int(raw))
    except Exception:
        return 60


# ================================ OAuth 失败短冷却 ================================ #
# 目的不是长期缓存失败，而是避免未授权 / 403 / 429 用户每次查询都固定打两跳。
# `no_oauth_token` 不做激进负缓存，这样用户刚完成 `/授权` 后更容易立即生效。
_OAUTH_BYPASS_CACHE: dict[tuple[str, str, str], float] = {}


def _oauth_bypass_key(qid: str, region: str, uid: str) -> tuple[str, str, str]:
    return (str(qid), str(region), str(uid))


def _should_bypass_oauth(qid: str, region: str, uid: str) -> bool:
    return _OAUTH_BYPASS_CACHE.get(_oauth_bypass_key(qid, region, uid), 0) > time.time()


def _mark_oauth_bypass(qid: str, region: str, uid: str):
    cooldown = _suite_oauth_fail_cooldown_sec()
    if cooldown <= 0:
        return
    _OAUTH_BYPASS_CACHE[_oauth_bypass_key(qid, region, uid)] = time.time() + cooldown


def clear_oauth_runtime_cache(qid: str | None = None):
    """清理当前 Bot 进程内的 suite OAuth 短冷却缓存。"""
    if qid is None:
        _OAUTH_BYPASS_CACHE.clear()
        return

    qid = str(qid)
    for key in list(_OAUTH_BYPASS_CACHE.keys()):
        if key[0] == qid:
            _OAUTH_BYPASS_CACHE.pop(key, None)


# ================================ 授权链接生成 ================================ #
# 当前这套 client 已按 confidential client 实际申请下来，
# 而上游文档给出的 confidential 换 token 示例不再要求 PKCE。
# 因此这里主动收敛为“只发 state，不再发 code_challenge”：
# `compat_code_verifier` 仍继续登记到云端 pending state 中，主要是为了让当前接口形状保持兼容，
# 避免这次热修还要同时改 Bot -> Cloud 的请求结构；它在当前 confidential 路径里不会真正发给上游。
def _generate_oauth_state() -> tuple[str, str]:
    state = secrets.token_urlsafe(32)
    compat_code_verifier = secrets.token_urlsafe(64)
    return state, compat_code_verifier


async def build_authorize_url(qid: str) -> str:
    """为指定 qid 生成 Haruki OAuth2 授权链接。"""
    if not _is_oauth_cloud_enabled():
        raise RuntimeError("OAuth2 功能未启用，请先检查 .env 与 sekai.yaml 配置")

    state, compat_code_verifier = _generate_oauth_state()
    async with get_client_session().post(
        f"{_cloud_base()}/oauth2/pending",
        json={
            "state": state,
            "qid": str(qid),
            "code_verifier": compat_code_verifier,
        },
        headers={"X-Sekai-Account-Token": _cloud_token()},
        verify_ssl=False,
    ) as response:
        if response.status != 200:
            body = await response.text()
            raise RuntimeError(f"向云端登记授权 state 失败: {response.status} {body}")

    params = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": _client_id(),
        "redirect_uri": _redirect_uri(),
        "scope": _scope(),
        "state": state,
    })
    return f"{_authorize_url()}?{params}"


# ================================ 云端 OAuth 代理请求 ================================ #
# Bot 不直接接触 access_token，只和自己的云端交互。
# 当前 Phase 1 只对 suite 开放代理，因此 data_type 白名单最终仍由云端控制。
def _build_oauth_game_data_params(
    uid: str,
    filter: list[str] | set[str] | None = None,
) -> dict[str, str]:
    """构建发往云端 OAuth 代理的查询参数。"""
    params = {"uid": uid}

    if not filter:
        return params

    if isinstance(filter, set):
        key_list = sorted(filter)
    else:
        key_list = list(filter)

    seen = set()
    key_list = [str(key).strip() for key in key_list]
    key_list = [key for key in key_list if key and not (key in seen or seen.add(key))]
    if key_list:
        params["key"] = ",".join(key_list)
    return params


async def get_oauth_game_data(
    qid: str,
    region: str,
    data_type: str,
    uid: str,
    filter: list[str] | set[str] | None = None,
) -> object | None:
    """
    通过云端代理获取 OAuth game-data。

    返回约定：
    - 成功：返回上游 JSON（可能是 dict，也可能是单字段投影后的标量）
    - 不可用 / 不应继续走 OAuth：返回 None，由调用层决定是否回退
    """
    if not _is_oauth_cloud_enabled():
        return None
    if _should_bypass_oauth(qid, region, uid):
        return None

    try:
        async with get_client_session().get(
            f"{_cloud_base()}/account/sekai/oauth-game-data/{region}/{qid}/{data_type}",
            params=_build_oauth_game_data_params(uid, filter),
            headers={"X-Sekai-Account-Token": _cloud_token()},
            verify_ssl=False,
        ) as response:
            try:
                detail = await response.json()
            except Exception:
                detail = await response.text()

            if response.status == 404:
                if isinstance(detail, dict) and detail.get("code") == "no_oauth_token":
                    return None
                _mark_oauth_bypass(qid, region, uid)
                return None

            if response.status in (403, 429):
                _mark_oauth_bypass(qid, region, uid)
                return None

            if response.status in (401, 500, 502, 503, 504):
                logger.warning(
                    f"[sekai] suite oauth proxy unavailable "
                    f"status={response.status} region={region} qid={qid} uid={uid}"
                )
                return None

            if response.status != 200:
                logger.warning(
                    f"[sekai] suite oauth proxy unexpected "
                    f"status={response.status} region={region} qid={qid} uid={uid}"
                )
                return None

            return detail
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        logger.warning(f"[sekai] suite oauth proxy request failed region={region} qid={qid} uid={uid}: {e}")
        return None
