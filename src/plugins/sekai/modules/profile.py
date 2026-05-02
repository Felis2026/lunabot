from ...utils import *
from ..common import *
from ..handler import *
from ..asset import *
from ..draw import *
from ..gameapi import get_gameapi_config, request_gameapi
from .. import account_api as cloud_account_api
from ..oauth import (
    _default_suite_source_policy,
    _normalize_suite_source_mode,
    build_authorize_url,
    clear_oauth_runtime_cache,
    get_oauth_game_data,
)
from .honor import compose_full_honor_image
from .resbox import get_res_box_info, get_res_icon
from ...utils.safety import *
from ...imgtool import shrink_image


SEKAI_PROFILE_DIR = f"{SEKAI_DATA_DIR}/profile"
profile_db = get_file_db(f"{SEKAI_PROFILE_DIR}/db.json", logger)
bind_history_db = get_file_db(f"{SEKAI_PROFILE_DIR}/bind_history.json", logger)
player_frame_db = get_file_db(f"{SEKAI_PROFILE_DIR}/player_frame.json", logger)

DAILY_BIND_LIMITS = config.item('bind.daily_limits')
TOTAL_BIND_LIMITS = config.item('bind.total_limits')


@dataclass
class PlayerAvatarInfo:
    card_id: int
    cid: int
    unit: str
    img: Image.Image

DEFAULT_DATA_MODE = 'latest'
VALID_DATA_MODES = ['latest', 'default', 'local', 'haruki']
VALID_SUITE_SOURCE_MODES = ['default', 'oauth', 'public']


# ================================ Suite 来源策略 ================================ #
# suite 已经不再复用旧 data_mode 的真实来源语义，而是改成独立的三态：
# - default：自己查自己时先走 OAuth，失败再回退 Public-API
# - oauth：自己查自己时只走 OAuth
# - public：统一只走 Public-API
#
# 但升级前写进本地 / 云端的旧值仍可能是 latest / local / haruki，
# 所以读取时必须集中做一次兼容归一化，不能把判断散落到各条命令里。
def get_user_suite_source_mode(ctx: SekaiHandlerContext, qid: int) -> str:
    """读取用户的 suite 来源模式，并兼容旧 data_mode 残留值。"""
    if state := _get_cloud_state(ctx, qid):
        pref = state.get("preferences", {})
        if isinstance(pref, dict):
            if pref.get("suite_source_mode") is not None:
                return _normalize_suite_source_mode(pref.get("suite_source_mode"))
            if pref.get("data_mode") is not None:
                return _normalize_suite_source_mode(pref.get("data_mode"))

    suite_source_modes = profile_db.get("suite_source_modes", {})
    raw_mode = suite_source_modes.get(ctx.region, {}).get(str(qid))
    if raw_mode is not None:
        return _normalize_suite_source_mode(raw_mode)

    legacy_data_modes = profile_db.get("data_modes", {})
    raw_mode = legacy_data_modes.get(ctx.region, {}).get(str(qid))
    if raw_mode is not None:
        return _normalize_suite_source_mode(raw_mode)

    return _default_suite_source_policy()


def can_use_oauth_suite(ctx: SekaiHandlerContext, qid: int, uid: str) -> bool:
    """
    只有“当前发起者查自己的 suite 远端数据”才允许先走 OAuth。

    关键约束：
    - `public` 模式下直接关闭 OAuth
    - `u2 / u3` 这类“查自己其他绑定”允许走 OAuth
    - `@别人`、直接输入游戏 UID、管理员代查都不允许走 OAuth
    """
    if get_user_suite_source_mode(ctx, qid) == 'public':
        return False

    requester_qid = str(ctx.user_id)
    if str(qid) != requester_qid:
        return False

    uid_arg = (ctx.uid_arg or '').strip()
    if uid_arg.startswith('@'):
        return False
    if uid_arg.isdigit():
        return False

    own_uids = set()
    for index in range(get_player_bind_count(ctx, qid)):
        own_uid = get_player_bind_id(ctx, qid, check_bind=False, index=index)
        if own_uid:
            own_uids.add(str(own_uid))
    return str(uid) in own_uids


def get_suite_source_display_name(source: str | None) -> str:
    """将 suite 的机器来源值映射成面向用户的展示名称。"""
    normalized = str(source or '').strip().lower()
    if normalized in {'haruki_oauth'}:
        return 'Haruki工具箱'
    if normalized in {'haruki_public', 'haruki', 'public', 'remote'}:
        return 'Haruki Public-API'
    return source or '?'


def normalize_suite_source_value(source: str | None, fallback: str) -> str:
    """统一 suite 的来源机器值，避免旧字段混进展示层。"""
    normalized = str(source or '').strip().lower()
    if normalized in {'haruki_oauth', 'oauth'}:
        return 'haruki_oauth'
    if normalized in {'haruki_public', 'haruki', 'public', 'remote'}:
        return 'haruki_public'
    return fallback


def get_suite_public_warning_text(ctx: SekaiHandlerContext, profile: dict | None) -> str:
    """只在 self-query 且最终落到 Public-API 时给出额外提醒。"""
    if not isinstance(profile, dict):
        return ""

    source = normalize_suite_source_value(profile.get('source'), 'haruki_public')
    if source != 'haruki_public':
        return ""

    requested_qid = str(profile.get('_requested_qid') or ctx.user_id)

    # Public-API / OAuth 兼容层有时只返回裁剪后的字段，`userGamedata.userId`
    # 可能并不稳定存在；但“是否属于自己查询”不能因此误判丢掉红字提醒。
    uid = (
        _get_nested_uid_from_suite_profile(profile)
        or str(profile.get('_requested_uid') or '').strip()
        or _get_requested_suite_uid_for_warning(ctx, requested_qid)
    )
    if uid and can_use_oauth_suite(ctx, requested_qid, str(uid)):
        return '公开API即将关闭，请尽快使用 /授权 使本Bot能够访问您的数据'
    return ""


def get_suite_expected_source_name(ctx: SekaiHandlerContext, qid: int, uid: str, suite_mode: str) -> str:
    """在请求失败时，根据当前策略推断状态页应展示的数据源名称。"""
    # `default` 模式下自己查自己时，本质上仍是“优先走 OAuth”。
    # 因此错误态不能再一刀切显示 Public-API，否则会出现：
    # - 实际已授权
    # - 但状态页标题还写着 Haruki Public-API
    # 这种明显互相打架的展示。
    if suite_mode != 'public' and can_use_oauth_suite(ctx, qid, uid):
        return 'Haruki工具箱'
    return 'Haruki Public-API'


# ================================ Suite 状态页授权摘要 ================================ #
# `/抓包状态` 面向用户时只补一行“授权状态”摘要，不直接展开 OAuth 技术细节。
# 这里的目标是回答：
# 1. 是否已经授权
# 2. 当前是否具备自动续期能力
# 3. 如果本次已经回退到 Public，是否要提示“当前使用公开接口”
def get_suite_oauth_status_text(
    ctx: SekaiHandlerContext,
    qid: int,
    uid: str,
    suite_mode: str,
    profile: dict | None,
    fetch_error: str = "",
) -> str:
    if not can_use_oauth_suite(ctx, qid, uid):
        return ""

    state = _get_cloud_state(ctx, qid)
    if not isinstance(state, dict):
        return ""

    if not bool(state.get("oauth_authorized", False)):
        return "授权状态: 未授权"

    # 这里不要再根据一次失败擅自推断 “OAuth 不可用”。
    # 当前 Bot 链路里很多 OAuth 失败会先被收口成 None 回退，
    # 如果在状态页继续脑补失败原因，很容易和真实运行状态打架。
    if fetch_error and suite_mode != 'public':
        if not bool(state.get("oauth_has_refresh_token", False)):
            return "授权状态: 已授权（需重新授权）"
        return "授权状态: 已授权"

    source = normalize_suite_source_value(
        profile.get("source") if isinstance(profile, dict) else None,
        "",
    )
    if source == "haruki_public" and suite_mode != "public":
        return "授权状态: 已授权（暂时异常，当前使用公开接口）"

    if not bool(state.get("oauth_has_refresh_token", False)):
        return "授权状态: 已授权（需重新授权）"

    return "授权状态: 已授权（自动续期正常）"


def format_suite_missing_fields_error(
    ctx: SekaiHandlerContext,
    profile: dict | None,
    missing_keys: list[str] | set[str] | tuple[str, ...],
) -> str:
    """统一格式化 suite 缺字段错误，避免各模块各写一份。"""
    source = get_suite_source_display_name(profile.get('source', '?') if isinstance(profile, dict) else '?')
    upload_time_ms = to_unix_millis(profile.get('upload_time') if isinstance(profile, dict) else None)
    update_time = datetime.fromtimestamp(upload_time_ms / 1000).strftime('%m-%d %H:%M:%S') if upload_time_ms else "?"
    missing_text = ', '.join(missing_keys)
    return (f"你的{get_region_name(ctx.region)}Suite抓包数据中缺少必要的字段: {missing_text}"
            f" (数据来源: {source} 更新时间: {update_time})")


def _get_nested_uid_from_suite_profile(profile: dict | None) -> str | None:
    """从 suite 数据里尽量提取 userId，用于 self-query 判断。"""
    if not isinstance(profile, dict):
        return None

    for path in (
        ('userGamedata', 'userId'),
        ('user', 'userId'),
        ('userGamedata', 'id'),
    ):
        cur = profile
        for key in path:
            if not isinstance(cur, dict):
                cur = None
                break
            cur = cur.get(key)
        if cur is not None:
            return str(cur)
    return None


def _get_requested_suite_uid_for_warning(ctx: SekaiHandlerContext, qid: int | str | None = None) -> str | None:
    """
    在 payload 缺少 userId 时，回退到“这次查询实际指向的 UID”。

    这样即使 Public-API 只返回了极少数字段，self-query 的隐私提醒也不会静默消失。
    """
    try:
        if qid is None:
            uid = get_player_bind_id(ctx, check_bind=False)
        else:
            uid = get_player_bind_id(ctx, int(qid), check_bind=False)
    except Exception:
        return None
    return str(uid) if uid else None


def _get_suite_display_name_for_card(ctx: SekaiHandlerContext, detail_profile: dict) -> str:
    """
    获取 suite 简卡使用的昵称展示文本。

    OAuth suite 当前实测不一定返回 `userGamedata.name`，因此这里必须做本地兜底，
    否则头像卡片会因为单个展示字段缺失而整张图失败。
    """
    game_data = detail_profile.get('userGamedata', {})
    if isinstance(game_data, dict):
        name = str(game_data.get('name') or '').strip()
        if name:
            return name

        uid = game_data.get('userId')
        if uid:
            return f"玩家 {process_hide_uid(ctx, uid, keep=6)}"
    return "未获取到昵称"


def _get_avatar_deck_from_detailed_profile(
    ctx: SekaiHandlerContext,
    detail_profile: dict,
) -> tuple[int | str, dict]:
    """
    从详细 profile 里解析头像卡片应使用的 deck。

    兼容约束：
    - 旧公开接口：优先读取 `userGamedata.deck`
    - 当前 OAuth suite：可能没有该字段，只能退化为首个可用 deck
    """
    user_gamedata = detail_profile.get('userGamedata')
    user_decks = detail_profile.get('userDecks')
    if not isinstance(user_gamedata, dict) or not isinstance(user_decks, list):
        raise ReplyException(format_suite_missing_fields_error(ctx, detail_profile, ['userGamedata', 'userDecks']))

    deck_id = user_gamedata.get('deck')
    if deck_id not in (None, ''):
        deck = find_by(user_decks, 'deckId', deck_id)
        if isinstance(deck, dict):
            return deck_id, deck

    # OAuth suite 当前没有稳定返回“当前选中卡组”字段时，退化到首个可用 deck，
    # 至少保证头像、头衔等前置信息能画出来，而不是整条命令直接 KeyError。
    for deck in user_decks:
        if isinstance(deck, dict) and deck.get('deckId') not in (None, ''):
            return deck['deckId'], deck

    raise ReplyException(format_suite_missing_fields_error(ctx, detail_profile, ['userDecks']))


def _get_legacy_suite_request_mode(suite_mode: str) -> str:
    """
    兼容仍在使用旧双源后端的部署。

    旧后端并不认识 `oauth / public`，只能退化成：
    - default -> default
    - oauth/public -> haruki
    """
    if suite_mode == 'default':
        return 'default'
    return 'haruki'


# ================================ 云端账号同步 ================================ #
# 云端账号服务是可选增强能力。
# 当环境变量未配置或远端异常时，Sekai 指令必须自动回退到本地数据路径。
def is_cloud_account_enabled() -> bool:
    return cloud_account_api.is_account_cloud_enabled()


def _get_cloud_state(ctx: SekaiHandlerContext, qid: int | str) -> dict | None:
    if not is_cloud_account_enabled():
        return None
    try:
        return cloud_account_api.get_state(ctx.region, qid)
    except Exception as e:
        logger.warning(f"[sekai] failed to load cloud account state region={ctx.region} qid={qid}: {e}")
        return None


def _get_cloud_snapshot(region: str | None = None) -> dict | None:
    if not is_cloud_account_enabled():
        return None
    try:
        return cloud_account_api.get_snapshot(region)
    except Exception as e:
        logger.warning(f"[sekai] failed to load cloud account snapshot region={region or '*'}: {e}")
        return None


# ================================ 快照兼容回退 ================================ #
# 优先读取云端快照，但也要兼容单实例部署下沿用的本地旧结构。
def _get_local_qid_blacklist() -> list[str]:
    return [str(qid) for qid in profile_db.get("qid_blacklist", [])]


def _get_uid_blacklist_snapshot() -> dict[str, list[str]]:
    if snapshot := _get_cloud_snapshot():
        data = snapshot.get("uid_blacklist", {})
        result: dict[str, list[str]] = {}
        if isinstance(data, dict):
            for region, uids in data.items():
                if isinstance(uids, list):
                    normalized = sorted({str(uid) for uid in uids if str(uid).strip()})
                    if normalized:
                        result[str(region)] = normalized
        return result
    legacy = sorted({str(item) for item in profile_db.get("blacklist", []) if str(item).strip()})
    return {"legacy": legacy} if legacy else {}


def _get_qid_blacklist_snapshot() -> list[str]:
    if snapshot := _get_cloud_snapshot():
        data = snapshot.get("qid_blacklist", [])
        if isinstance(data, list):
            return sorted({str(qid) for qid in data if str(qid).strip()}, key=lambda x: int(x))
    return sorted({str(qid) for qid in _get_local_qid_blacklist() if str(qid).strip()}, key=lambda x: int(x))


def get_uid_blacklist_snapshot() -> dict[str, list[str]]:
    return _get_uid_blacklist_snapshot()


def get_qid_blacklist_snapshot() -> list[str]:
    return _get_qid_blacklist_snapshot()


def _format_blacklist_entry_meta(entry: dict | None) -> str:
    if not isinstance(entry, dict):
        return "原因: 无"
    reason = (entry.get("reason") or "").strip() or "无"
    operator = (entry.get("operator") or "").strip()
    created_at = (entry.get("created_at") or "").strip()
    parts = [f"原因: {reason}"]
    if operator:
        parts.append(f"操作者: {operator}")
    if created_at:
        try:
            created_at = datetime.fromisoformat(created_at).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass
        parts.append(f"时间: {created_at}")
    return " | ".join(parts)


def _format_uid_blacklist_list_text() -> str:
    uid_blacklist = _get_uid_blacklist_snapshot()
    if not uid_blacklist:
        return "当前没有游戏ID黑名单"

    lines = ["当前游戏ID黑名单:"]
    for region in sorted(uid_blacklist.keys()):
        uids = uid_blacklist.get(region, [])
        if not uids:
            continue
        region_name = "未分区(本地兼容)" if region == "legacy" else get_region_name(region)
        lines.append(f"【{region_name}】{len(uids)} 个")
        for uid in uids:
            meta = "原因: 无"
            if region != "legacy" and is_cloud_account_enabled():
                try:
                    status = cloud_account_api.get_blacklist_status("uid", uid, region=region)
                    meta = _format_blacklist_entry_meta(status.get("entry"))
                except Exception as e:
                    logger.warning(f"[sekai] failed to format cloud uid blacklist meta region={region} uid={uid}: {e}")
            lines.append(f"{uid} | {meta}")
    return "\n".join(lines)


def _format_qid_blacklist_list_text() -> str:
    qids = _get_qid_blacklist_snapshot()
    if not qids:
        return "当前没有QQ黑名单"
    lines = [f"当前QQ黑名单: {len(qids)} 个"]
    for qid in qids:
        meta = "原因: 无"
        if is_cloud_account_enabled():
            try:
                status = cloud_account_api.get_blacklist_status("qid", qid)
                meta = _format_blacklist_entry_meta(status.get("entry"))
            except Exception as e:
                logger.warning(f"[sekai] failed to format cloud qid blacklist meta qid={qid}: {e}")
        lines.append(f"{qid} | {meta}")
    return "\n".join(lines)


def _resolve_uid_blacklist_region(uid: str, region: str | None = None) -> str | None:
    if region:
        return region
    hit_regions = []
    for r in ALL_SERVER_REGIONS:
        try:
            if validate_uid(SekaiHandlerContext.from_region(r), uid):
                hit_regions.append(r)
        except Exception:
            continue
    if len(hit_regions) == 1:
        return hit_regions[0]
    return None


def _get_bind_list_snapshot(region: str | None = None) -> dict:
    if snapshot := _get_cloud_snapshot(region):
        return snapshot.get("bind_list", {})
    bind_list = profile_db.get("bind_list", {})
    if region:
        return {region: bind_list.get(region, {})}
    return bind_list


def get_bind_list_snapshot(region: str | None = None) -> dict:
    return _get_bind_list_snapshot(region)


def add_uid_blacklist_entry(region: str, uid: str, reason: str = '', operator: str | None = None) -> bool:
    region = str(region).lower().strip()
    uid = str(uid).strip()
    assert region in ALL_SERVER_REGIONS, f'未知区服: {region}'
    assert validate_uid(SekaiHandlerContext.from_region(region), uid), f'无效的{region.upper()}游戏ID: {uid}'
    if is_cloud_account_enabled():
        status = cloud_account_api.get_blacklist_status('uid', uid, region=region)
        if status.get('active'):
            return False
        cloud_account_api.add_blacklist('uid', uid, region=region, reason=reason or None, operator=operator)
        return True
    blacklist = [str(item) for item in profile_db.get('blacklist', [])]
    if uid in blacklist:
        return False
    blacklist.append(uid)
    profile_db.set('blacklist', blacklist)
    return True


def remove_uid_blacklist_entry(region: str, uid: str, operator: str | None = None) -> bool:
    region = str(region).lower().strip()
    uid = str(uid).strip()
    assert region in ALL_SERVER_REGIONS, f'未知区服: {region}'
    if is_cloud_account_enabled():
        status = cloud_account_api.get_blacklist_status('uid', uid, region=region)
        if not status.get('active'):
            return False
        removed = cloud_account_api.remove_blacklist('uid', uid, region=region, operator=operator)
        return removed is not None
    blacklist = [str(item) for item in profile_db.get('blacklist', [])]
    if uid not in blacklist:
        return False
    blacklist.remove(uid)
    profile_db.set('blacklist', blacklist)
    return True


def add_qid_blacklist_entry(qid: str | int, reason: str = '', operator: str | None = None) -> bool:
    qid = str(qid).strip()
    assert qid.isdigit(), f'无效的QQ号: {qid}'
    if is_cloud_account_enabled():
        status = cloud_account_api.get_blacklist_status('qid', qid)
        if status.get('active'):
            return False
        cloud_account_api.add_blacklist('qid', qid, reason=reason or None, operator=operator)
        return True
    blacklist = _get_local_qid_blacklist()
    if qid in blacklist:
        return False
    blacklist.append(qid)
    profile_db.set('qid_blacklist', blacklist)
    return True


def remove_qid_blacklist_entry(qid: str | int, operator: str | None = None) -> bool:
    qid = str(qid).strip()
    assert qid.isdigit(), f'无效的QQ号: {qid}'
    if is_cloud_account_enabled():
        status = cloud_account_api.get_blacklist_status('qid', qid)
        if not status.get('active'):
            return False
        removed = cloud_account_api.remove_blacklist('qid', qid, operator=operator)
        return removed is not None
    blacklist = _get_local_qid_blacklist()
    if qid not in blacklist:
        return False
    blacklist.remove(qid)
    profile_db.set('qid_blacklist', blacklist)
    return True


def _parse_uid_blacklist_args(args: str) -> tuple[str, str, str]:
    parts = args.strip().split(maxsplit=2)
    assert_and_reply(parts, "请提供要操作的游戏ID")

    explicit_region = None
    uid = ""
    reason = ""

    if parts[0].lower() in ALL_SERVER_REGIONS:
        explicit_region = parts[0].lower()
        assert_and_reply(len(parts) >= 2, "请在区服后提供游戏ID")
        uid = parts[1].strip()
        reason = parts[2].strip() if len(parts) >= 3 else ""
    else:
        uid = parts[0].strip()
        reason = parts[1].strip() if len(parts) >= 2 else ""

    target_region = _resolve_uid_blacklist_region(uid, explicit_region)
    assert_and_reply(
        target_region is not None,
        f"无法自动判断游戏ID {uid} 的区服，请使用“/pjsk blacklist add cn {uid} 原因”或“/pjsk blacklist add jp {uid} 原因”",
    )
    assert_and_reply(
        validate_uid(SekaiHandlerContext.from_region(target_region), uid),
        f"无效的{target_region.upper()}游戏ID: {uid}",
    )
    return target_region, uid, reason


def _parse_qid_blacklist_args(args: str) -> tuple[str, str]:
    parts = args.strip().split(maxsplit=1)
    assert_and_reply(parts, "请提供要操作的QQ号")
    qid = parts[0].strip()
    assert_and_reply(qid.isdigit(), f"无效的QQ号: {qid}")
    reason = parts[1].strip() if len(parts) >= 2 else ""
    return qid, reason


@dataclass
class VerifyCode:
    region: str
    qid: int
    uid: int
    expire_time: datetime
    verify_code: str

VERIFY_CODE_EXPIRE_TIME = timedelta(minutes=30)
_region_qid_verify_codes: Dict[str, Dict[str, VerifyCode]] = {}
verify_rate_limit = RateLimit(file_db, logger, 10, 'd', rate_limit_name='pjsk验证')


@dataclass
class ProfileBgSettings:
    image: Image.Image
    blur: int = None
    alpha: int = None
    vertical: bool = False

PROFILE_BG_IMAGE_PATH = f"{SEKAI_PROFILE_DIR}/profile_bg/" + "{region}/{uid}.jpg"
profile_bg_settings_db = get_file_db(f"{SEKAI_PROFILE_DIR}/profile_bg_settings.json", logger)
profile_bg_upload_rate_limit = RateLimit(file_db, logger, 10, 'd', rate_limit_name='个人信息背景上传')

PROFILE_HORIZONTAL_KEYWORDS = ('横屏', '横向', '横版',)
PROFILE_VERTICAL_KEYWORDS = ('竖屏', '竖向', '竖版', '纵向',)


# ======================= 卡牌逻辑（防止循环依赖） ======================= #

CARD_ICON_CACHE_RES = 128 * 128

# 判断卡牌是否有after_training模式
def has_after_training(card):
    return card['cardRarityType'] in ["rarity_3", "rarity_4"]

# 判断卡牌是否只有after_training模式
def only_has_after_training(card):
    return card.get('initialSpecialTrainingStatus') == 'done'

# 获取角色卡牌缩略图
async def get_card_thumbnail(ctx: SekaiHandlerContext, cid: int, after_training: bool, high_res: bool=False):
    image_type = "after_training" if after_training else "normal"
    card = await ctx.md.cards.find_by_id(cid)
    assert_and_reply(card, f"找不到ID为{cid}的卡牌")
    img_cache_kwargs = {}
    if not high_res:
        img_cache_kwargs = {'use_img_cache': True, 'img_cache_max_res': CARD_ICON_CACHE_RES }
    return await ctx.rip.img(f"thumbnail/chara_rip/{card['assetbundleName']}_{image_type}.png", **img_cache_kwargs)

# 获取角色卡牌完整缩略图（包括边框、星级等）
async def get_card_full_thumbnail(
    ctx: SekaiHandlerContext, 
    card_or_card_id: Dict, 
    after_training: bool=None, 
    pcard: Dict=None, 
    custom_text: str=None,
    high_res: bool=False,
):
    if isinstance(card_or_card_id, int):
        card = await ctx.md.cards.find_by_id(card_or_card_id)
        assert_and_reply(card, f"找不到ID为{card_or_card_id}的卡牌")
    else:
        card = card_or_card_id
    cid = card['id']

    if not pcard:
        after_training = after_training and has_after_training(card)
        rare_image_type = "after_training" if after_training else "normal"
    else:
        after_training = pcard['defaultImage'] == "special_training"
        rare_image_type = "after_training" if pcard['specialTrainingStatus'] == "done" else "normal"

    # 如果没有指定pcard则尝试使用缓存
    if not pcard:
        image_type = "after_training" if after_training else "normal"
        cache_path = f"{SEKAI_ASSET_DIR}/card_full_thumbnail/{ctx.region}/{cid}_{image_type}.png"
        try: return open_image(cache_path)
        except: pass

    img = await get_card_thumbnail(ctx, cid, after_training, high_res=high_res)
    ok_to_cache = (img != UNKNOWN_IMG)
    img = img.resize((128, 128), Image.BICUBIC)

    def draw(img: Image.Image, card):
        attr = card['attr']
        rare = card['cardRarityType']
        frame_img = ctx.static_imgs.get(f"card/frame_{rare}.png")
        attr_img = ctx.static_imgs.get(f"card/attr_{attr}.png")
        if rare == "rarity_birthday":
            rare_img = ctx.static_imgs.get(f"card/rare_birthday.png")
            rare_num = 1
        else:
            rare_img = ctx.static_imgs.get(f"card/rare_star_{rare_image_type}.png") 
            rare_num = int(rare.split("_")[1])

        img_w, img_h = img.size

        # 如果是profile卡片则绘制等级/加成
        if pcard:
            if custom_text is not None:
                draw = ImageDraw.Draw(img)
                draw.rectangle((0, img_h - 24, img_w, img_h), fill=(70, 70, 100, 255))
                draw.text((6, img_h - 31), custom_text, font=get_font(DEFAULT_BOLD_FONT, 20), fill=WHITE)
            else:
                level = pcard['level']
                draw = ImageDraw.Draw(img)
                draw.rectangle((0, img_h - 24, img_w, img_h), fill=(70, 70, 100, 255))
                draw.text((6, img_h - 31), f"Lv.{level}", font=get_font(DEFAULT_BOLD_FONT, 20), fill=WHITE)
            
        # 绘制边框
        frame_img = frame_img.resize((img_w, img_h))
        img.paste(frame_img, (0, 0), frame_img)
        # 绘制特训等级
        if pcard:
            rank = pcard['masterRank']
            if rank:
                rank_img = ctx.static_imgs.get(f"card/train_rank_{rank}.png")
                rank_img = rank_img.resize((int(img_w * 0.35), int(img_h * 0.35)))
                rank_img_w, rank_img_h = rank_img.size
                img.paste(rank_img, (img_w - rank_img_w, img_h - rank_img_h), rank_img)
        # 左上角绘制属性
        attr_img = attr_img.resize((int(img_w * 0.22), int(img_h * 0.25)))
        img.paste(attr_img, (1, 0), attr_img)
        # 左下角绘制稀有度
        hoffset, voffset = 6, 6 if not pcard else 24
        scale = 0.17 if not pcard else 0.15
        rare_img = rare_img.resize((int(img_w * scale), int(img_h * scale)))
        rare_w, rare_h = rare_img.size
        for i in range(rare_num):
            img.paste(rare_img, (hoffset + rare_w * i, img_h - rare_h - voffset), rare_img)
        mask = Image.new('L', (img_w, img_h), 0)
        draw = ImageDraw.Draw(mask)
        draw.rounded_rectangle((0, 0, img_w, img_h), radius=10, fill=255)
        img.putalpha(mask)
        return img
    
    img = await run_in_pool(draw, img, card)

    if not pcard and ok_to_cache:
        create_parent_folder(cache_path)
        img.save(cache_path)

    return img

# 获取卡牌所属团名（return_support控制VS是否返回对应的所属团）
async def get_unit_by_card_id(ctx: SekaiHandlerContext, card_id: int, return_support: bool = True) -> str:
    card = await ctx.md.cards.find_by_id(card_id)
    if not card: raise Exception(f"卡牌ID={card_id}不存在")
    chara_unit = get_unit_by_chara_id(card['characterId'])
    if not return_support or chara_unit != 'piapro':
        return chara_unit
    return card['supportUnit'] if card['supportUnit'] != "none" else "piapro"


# ======================= 帐号相关 ======================= #

# 为兼容原本数据格式，用户绑定数据可能是字符串或字符串列表
def to_list(s: list | Any) -> list:
    if isinstance(s, list):
        return s
    return [s]

# 验证uid
def validate_uid(ctx: SekaiHandlerContext, uid: str) -> bool:
    uid = str(uid)
    if not (13 <= len(uid) <= 20) or not uid.isdigit():
        return False
    reg_time = get_register_time(ctx.region, uid)
    if not reg_time or not (datetime.strptime("2020-09-01", "%Y-%m-%d") <= reg_time <= datetime.now()):
        return False
    return True

# 获取用户绑定的账号数量
def get_player_bind_count(ctx: SekaiHandlerContext, qid: int) -> int:
    if state := _get_cloud_state(ctx, qid):
        return len(state.get("bindings", []))
    bind_list: Dict[str, str | list[str]] = profile_db.get("bind_list", {}).get(ctx.region, {})
    uids = to_list(bind_list.get(str(qid), []))
    return len(uids)

# 获取qq用户绑定的游戏id，如果qid=None则使用ctx.uid_arg获取用户id，index=None获取主绑定账号
def get_player_bind_id(ctx: SekaiHandlerContext, qid: int = None, check_bind=True, index: int | None=None) -> str:
    is_super = check_superuser(ctx.event) if ctx.event else False
    region_name = get_region_name(ctx.region)
    
    bind_list: Dict[str, str | list[str]] = profile_db.get("bind_list", {}).get(ctx.region, {})
    main_bind_list: Dict[str, str] = profile_db.get("main_bind_list", {}).get(ctx.region, {})
    state_qid = str(qid) if qid is not None else str(ctx.user_id)
    cloud_state = _get_cloud_state(ctx, state_qid) if (qid or not ctx.uid_arg) else None

    def get_uid_by_index(qid: str, index: int) -> str | None:
        if cloud_state and qid == state_qid:
            uids = [str(item) for item in cloud_state.get("bindings", [])]
        else:
            uids = bind_list.get(qid, [])
        if not uids:
            return None
        uids = to_list(uids)
        assert_and_reply(0 <= index < len(uids), f"指定的账号序号大于已绑定的{region_name}账号数量({len(uids)})")
        return uids[index]

    # 指定qid/没有ctx.uid_arg的情况则直接获取qid绑定的账号
    if qid or not ctx.uid_arg:
        qid = str(qid) if qid is not None else str(ctx.user_id)
        if not is_super:
            assert_and_reply(not check_qid_in_blacklist(qid), f"该QQ号({qid})已被拉入黑名单")
        if index is None:
            uid = (cloud_state or {}).get("main_uid") or main_bind_list.get(qid, None) or get_uid_by_index(qid, 0)
        else:
            uid = get_uid_by_index(qid, index)
    # 从ctx.uid_arg中获取
    else:
        if ctx.uid_arg.startswith('u'):
            index = int(ctx.uid_arg[1:]) - 1
            uid = get_uid_by_index(str(ctx.user_id), index)
        elif ctx.uid_arg.startswith('@'):
            assert_and_reply(is_super, "仅bot管理可直接@指定QQ号")
            at_qid = int(ctx.uid_arg[1:])
            uid = get_player_bind_id(ctx, at_qid, check_bind)
        else:
            assert_and_reply(is_super, "仅bot管理可直接指定游戏ID")
            uid = ctx.uid_arg
            if not validate_uid(ctx, uid):
                raise ReplyException(f"指定的游戏ID {uid} 不是有效的{region_name}游戏ID")

    if check_bind and uid is None:
        region = "" if ctx.region == "jp" else ctx.region
        raise ReplyException(f"请使用\"/{region}绑定 你的游戏ID\"绑定账号")
    if not is_super:
        assert_and_reply(not check_uid_in_blacklist(uid, ctx.region), f"该游戏ID({uid})已被拉入黑名单")
    return uid

# 获取某个id在用户绑定的账号中的索引，找不到返回None
def get_player_bind_id_index(ctx: SekaiHandlerContext, qid: str, uid: str) -> int | None:
    if state := _get_cloud_state(ctx, qid):
        try:
            return [str(item) for item in state.get("bindings", [])].index(str(uid))
        except ValueError:
            return None
    bind_list: Dict[str, str | list[str]] = profile_db.get("bind_list", {}).get(ctx.region, {})
    uids = to_list(bind_list.get(str(qid), []))
    try:
        return uids.index(str(uid))
    except ValueError:
        return None

# 为用户绑定游戏id，该函数仅判断uid是否重复，绑定的uid需要已经验证合法，返回额外信息
def add_player_bind_id(ctx: SekaiHandlerContext, qid: str, uid: str, set_main: bool) -> str:
    all_bind_list: Dict[str, str | list[str]] = profile_db.get("bind_list", {})
    all_main_bind_list: Dict[str, str] = profile_db.get("main_bind_list", {})
    qid = str(qid)
    region = ctx.region
    region_name = get_region_name(region)
    additional_info = ""

    if is_cloud_account_enabled():
        state = _get_cloud_state(ctx, qid) or {
            "bindings": [],
            "main_uid": None,
        }
        uids = [str(item) for item in state.get("bindings", [])]
        if uid not in uids:
            total_bind_limit = TOTAL_BIND_LIMITS.get().get(ctx.region, 1e9)
            while len(uids) >= total_bind_limit:
                removed_uid = uids.pop(0)
                try:
                    cloud_account_api.remove_binding(region, qid, removed_uid, operator=str(ctx.user_id))
                except Exception as e:
                    raise ReplyException(f"云端解绑旧账号失败: {e}")
                additional_info += f"你绑定的{region_name}账号数量已达上限({total_bind_limit})，已自动解绑最早绑定的账号\n"
            try:
                state = cloud_account_api.add_binding(region, qid, uid, set_main=set_main, operator=str(ctx.user_id))
            except Exception as e:
                raise ReplyException(f"云端绑定失败: {e}")
            uids = [str(item) for item in state.get("bindings", [])]
        elif set_main:
            try:
                state = cloud_account_api.set_main_binding(region, qid, uid, operator=str(ctx.user_id))
            except Exception as e:
                raise ReplyException(f"云端设置主绑定失败: {e}")
            uids = [str(item) for item in state.get("bindings", [])]
        if set_main and uid in uids:
            uid_index = uids.index(uid) + 1
            additional_info += f"已将该账号u{uid_index}设为你的{region_name}主账号\n"
        return additional_info.strip()

    if region not in all_bind_list:
        all_bind_list[region] = {}
    if region not in all_main_bind_list:
        all_main_bind_list[region] = {}

    uids = to_list(all_bind_list[region].get(qid, []))
    if uid not in uids:
        total_bind_limit = TOTAL_BIND_LIMITS.get().get(ctx.region, 1e9)
        if len(uids) >= total_bind_limit:
            while len(uids) >= total_bind_limit:
                uids.pop(0)
            additional_info += f"你绑定的{region_name}账号数量已达上限({total_bind_limit})，已自动解绑最早绑定的账号\n"
        uids.append(uid)
        
        all_bind_list[region][qid] = uids
        profile_db.set("bind_list", all_bind_list)
        logger.info(f"为 {qid} 绑定 {region_name}账号: {uid}")
    else:
        logger.info(f"为 {qid} 绑定 {region_name}账号: {uid} 已存在，跳过绑定")

    if set_main:
        all_main_bind_list[region][qid] = uid
        profile_db.set("main_bind_list", all_main_bind_list)
        uid_index = uids.index(uid) + 1
        additional_info += f"已将该账号u{uid_index}设为你的{region_name}主账号\n"
        logger.info(f"为 {qid} 设定 {region_name}主账号: {uid}")

    return additional_info.strip()

# 使用索引解除绑定，返回信息，index为None则解除主绑定账号
def remove_player_bind_id(ctx: SekaiHandlerContext, qid: str, index: int | None) -> str:
    all_bind_list: Dict[str, str | list[str]] = profile_db.get("bind_list", {})
    all_main_bind_list: Dict[str, str] = profile_db.get("main_bind_list", {})
    qid = str(qid)
    region = ctx.region
    region_name = get_region_name(region)
    ret_info = ""

    if is_cloud_account_enabled():
        state = _get_cloud_state(ctx, qid) or {
            "bindings": [],
            "main_uid": None,
        }
        uids = [str(item) for item in state.get("bindings", [])]
        assert_and_reply(uids, f"你还没有绑定任何{region_name}账号")
        assert_and_reply(index is None or index < 1e9, f"需要指定账号序号（按绑定时间顺序）而不是账号ID")
        if index is not None:
            assert_and_reply(0 <= index < len(uids), f"指定的账号序号大于已绑定的{region_name}账号数量({len(uids)})")
            removed_uid = uids[index]
        else:
            removed_uid = state.get("main_uid") or uids[0]
        try:
            next_state = cloud_account_api.remove_binding(region, qid, removed_uid, operator=str(ctx.user_id))
        except Exception as e:
            raise ReplyException(f"云端解绑失败: {e}")
        next_uids = [str(item) for item in next_state.get("bindings", [])]
        ret_info += f"已解除绑定你的{region_name}账号{process_hide_uid(ctx, removed_uid, keep=6)}\n"
        if next_uids and next_state.get("main_uid") and next_state.get("main_uid") != removed_uid:
            ret_info += f"已将你的{region_name}主账号切换为当前第一个账号({process_hide_uid(ctx, next_state.get("main_uid"), keep=6)})\n"
        elif not next_uids:
            ret_info += f"你目前没有绑定任何{region_name}账号，主账号已清除\n"
        return ret_info.strip()


    if region not in all_bind_list:
        all_bind_list[region] = {}
    if region not in all_main_bind_list:
        all_main_bind_list[region] = {}

    uids = to_list(all_bind_list[region].get(qid, []))
    assert_and_reply(uids, f"你还没有绑定任何{region_name}账号")
    assert_and_reply(index < 1e9, f"需要指定账号序号（按绑定时间顺序）而不是账号ID")

    if index is not None:
        assert_and_reply(0 <= index < len(uids), f"指定的账号序号大于已绑定的{region_name}账号数量({len(uids)})")
        removed_uid = uids.pop(index)
    else:
        main_bind_uid = get_player_bind_id(ctx, qid)
        uids.remove(main_bind_uid)
        removed_uid = main_bind_uid

    all_bind_list[region][qid] = uids
    profile_db.set("bind_list", all_bind_list)
    logger.info(f"为 {qid} 解除绑定 {region_name}账号: {removed_uid}")

    ret_info += f"已解除绑定你的{region_name}账号{process_hide_uid(ctx, removed_uid, keep=6)}\n"

    if all_main_bind_list[region].get(qid, None) == removed_uid:
        if uids:
            all_main_bind_list[region][qid] = uids[0]
            ret_info += f"已将你的{region_name}主账号切换为当前第一个账号({process_hide_uid(ctx, uids[0], keep=6)})\n"
            logger.info(f"为 {qid} 切换 {region_name}主账号: {uids[0]}")
        else:
            all_main_bind_list[region].pop(qid, None)
            ret_info += f"你目前没有绑定任何{region_name}账号，主账号已清除\n"
            logger.info(f"为 {qid} 清除 {region_name}主账号")
        profile_db.set("main_bind_list", all_main_bind_list)

    return ret_info.strip()

# 使用索引修改主绑定账号，返回信息
def set_player_main_bind_id(ctx: SekaiHandlerContext, qid: str, index: int) -> str:
    all_bind_list: Dict[str, str | list[str]] = profile_db.get("bind_list", {})
    all_main_bind_list: Dict[str, str] = profile_db.get("main_bind_list", {})
    qid = str(qid)
    region = ctx.region
    region_name = get_region_name(region)
    if is_cloud_account_enabled():
        state = _get_cloud_state(ctx, qid) or {"bindings": []}
        uids = [str(item) for item in state.get("bindings", [])]
        assert_and_reply(uids, f"你还没有绑定任何{region_name}账号")
        assert_and_reply(index < 1e9, f"需要指定账号序号（按绑定时间顺序）而不是账号ID")
        assert_and_reply(0 <= index < len(uids), f"指定的账号序号大于已绑定的{region_name}账号数量({len(uids)})")
        new_main_uid = uids[index]
        try:
            cloud_account_api.set_main_binding(region, qid, new_main_uid, operator=str(ctx.user_id))
        except Exception as e:
            raise ReplyException(f"云端设置主绑定失败: {e}")
        return f"已将你的{region_name}主账号修改为{process_hide_uid(ctx, new_main_uid, keep=6)}"

    if region not in all_bind_list:
        all_bind_list[region] = {}
    if region not in all_main_bind_list:
        all_main_bind_list[region] = {}

    uids = to_list(all_bind_list[region].get(qid, []))
    assert_and_reply(uids, f"你还没有绑定任何{region_name}账号")
    assert_and_reply(index < 1e9, f"需要指定账号序号（按绑定时间顺序）而不是账号ID")
    assert_and_reply(0 <= index < len(uids), f"指定的账号序号大于已绑定的{region_name}账号数量({len(uids)})")

    new_main_uid = uids[index]
    all_main_bind_list[region][qid] = new_main_uid
    profile_db.set("main_bind_list", all_main_bind_list)

    return f"已将你的{region_name}主账号修改为{process_hide_uid(ctx, new_main_uid, keep=6)}"

# 使用索引交换账号顺序
def swap_player_bind_id(ctx: SekaiHandlerContext, qid: str, index1: int, index2: int) -> str:
    all_bind_list: Dict[str, str | list[str]] = profile_db.get("bind_list", {})
    qid = str(qid)
    region = ctx.region
    region_name = get_region_name(region)

    if is_cloud_account_enabled():
        state = _get_cloud_state(ctx, qid) or {"bindings": []}
        uids = [str(item) for item in state.get("bindings", [])]
        assert_and_reply(uids, f"你还没有绑定任何{region_name}账号")
        assert_and_reply(index1 < 1e9, f"需要指定账号序号（按绑定时间顺序）而不是账号ID")
        assert_and_reply(index2 < 1e9, f"需要指定账号序号（按绑定时间顺序）而不是账号ID")
        assert_and_reply(0 <= index1 < len(uids), f"指定的账号序号1大于已绑定的{region_name}账号数量({len(uids)})")
        assert_and_reply(0 <= index2 < len(uids), f"指定的账号序号2大于已绑定的{region_name}账号数量({len(uids)})")
        try:
            cloud_account_api.swap_bindings(region, qid, uids[index1], uids[index2], operator=str(ctx.user_id))
        except Exception as e:
            raise ReplyException(f"云端交换绑定顺序失败: {e}")
        return f"已交换你的{region_name}第{index1 + 1}个账号和第{index2 + 1}个账号的顺序"

    if region not in all_bind_list:
        all_bind_list[region] = {}

    uids = to_list(all_bind_list[region].get(qid, []))
    assert_and_reply(uids, f"你还没有绑定任何{region_name}账号")
    assert_and_reply(index1 < 1e9, f"需要指定账号序号（按绑定时间顺序）而不是账号ID")
    assert_and_reply(index2 < 1e9, f"需要指定账号序号（按绑定时间顺序）而不是账号ID")
    assert_and_reply(0 <= index1 < len(uids), f"指定的账号序号1大于已绑定的{region_name}账号数量({len(uids)})")
    assert_and_reply(0 <= index2 < len(uids), f"指定的账号序号2大于已绑定的{region_name}账号数量({len(uids)})")

    uids[index1], uids[index2] = uids[index2], uids[index1]
    all_bind_list[region][qid] = uids
    profile_db.set("bind_list", all_bind_list)

    return f"""
已将你绑定的{region_name}第{index1 + 1}个账号序号和第{index2 + 1}个账号交换顺序
该指令仅影响索引查询(u{index1 + 1}、u{index2 + 1})，修改默认查询账号请使用"/主账号"
""".strip()


# 验证用户游戏帐号
async def verify_user_game_account(ctx: SekaiHandlerContext, triggered_by_not_verified: bool = False):
    verified_uids = get_user_verified_uids(ctx)
    uid = get_player_bind_id(ctx)
    assert_and_reply(uid not in verified_uids, f"你当前绑定的{get_region_name(ctx.region)}帐号已经验证过")

    def generate_verify_code() -> str:
        while True:
            code = str(random.randint(1000, 9999))
            code = '/'.join(code)
            hit = False
            for codes in _region_qid_verify_codes.values():
                if any(info.verify_code == code for info in codes.values()):
                    hit = True
                    break
            if hit:
                continue
            return code
    
    qid = ctx.user_id
    if ctx.region not in _region_qid_verify_codes:
        _region_qid_verify_codes[ctx.region] = {}

    info = None
    err_msg = ""
    if qid in _region_qid_verify_codes[ctx.region]:
        info = _region_qid_verify_codes[ctx.region][qid]
        if info.expire_time < datetime.now():
            err_msg = f"你的上次验证已过期\n"
        if info.uid != uid:
            err_msg = f"开始验证时绑定的帐号与当前绑定帐号不一致\n"

    if triggered_by_not_verified:
        err_msg = f"该功能需要验证你的游戏账号\n"

    if err_msg:
        _region_qid_verify_codes[ctx.region].pop(qid, None)
        info = None
    
    # 首次验证
    if not info:
        info = VerifyCode(
            region=ctx.region,
            qid=qid,
            uid=uid,
            verify_code=generate_verify_code(),
            expire_time=datetime.now() + VERIFY_CODE_EXPIRE_TIME,
        )
        _region_qid_verify_codes[ctx.region][qid] = info
        raise ReplyException(f"""
{err_msg}请在你当前绑定的{get_region_name(ctx.region)}帐号的名片简介末尾输入验证码(不要去掉斜杠):
{info.verify_code}
编辑后退出名片界面保存，然后在{get_readable_timedelta(VERIFY_CODE_EXPIRE_TIME)}内发送\"/{ctx.region}pjsk验证\"完成验证
""".strip())
    
    profile = await get_basic_profile(ctx, info.uid, use_cache=False, use_remote_cache=False)
    word: str = profile['userProfile'].get('word', '').strip()

    assert_and_reply(word.endswith(info.verify_code), f"""
验证失败，从你绑定的{get_region_name(ctx.region)}帐号留言末尾没有获取到验证码\"{info.verify_code}\"，请重试（验证码未改变）
""".strip())

    try:
        # 验证成功
        if is_cloud_account_enabled():
            try:
                cloud_account_api.add_verified_account(ctx.region, qid, info.uid, operator=str(ctx.user_id))
            except Exception as e:
                raise ReplyException(f"云端写入验证状态失败: {e}")
        else:
            verify_accounts = profile_db.get(f"verify_accounts_{ctx.region}", {})
            verify_accounts.setdefault(str(qid), []).append(info.uid)
            profile_db.set(f"verify_accounts_{ctx.region}", verify_accounts)
        raise ReplyException(f"验证成功！使用\"/{ctx.region}pjsk验证列表\"可以查看你验证过的游戏ID")
    finally:
        _region_qid_verify_codes[ctx.region].pop(qid, None)

# 获取用户验证过的游戏ID列表
def get_user_verified_uids(ctx: SekaiHandlerContext) -> List[str]:
    if state := _get_cloud_state(ctx, ctx.user_id):
        return [str(uid) for uid in state.get("verified_uids", [])]
    return profile_db.get_copy(f"verify_accounts_{ctx.region}", {}).get(str(ctx.user_id), [])

# 获取游戏id并检查用户是否验证过当前的游戏id，失败抛出异常
async def get_uid_and_check_verified(ctx: SekaiHandlerContext, force: bool = False) -> str:
    uid = get_player_bind_id(ctx)
    if not force:
        verified_uids = get_user_verified_uids(ctx)
        if uid not in verified_uids:
            await verify_user_game_account(ctx, triggered_by_not_verified=True)
            # 正常情况下不会往下走
            assert_and_reply(uid in verified_uids, f"""
该功能需要验证你的游戏帐号
请使用"/{ctx.region}pjsk验证"进行验证，使用"/{ctx.region}pjsk验证列表"查看你验证过的游戏ID
""".strip())
    return uid


# 检测游戏id是否在黑名单中
def check_uid_in_blacklist(uid: str, region: str | None = None) -> bool:
    if is_cloud_account_enabled():
        target_region = _resolve_uid_blacklist_region(str(uid), region)
        if target_region:
            try:
                status = cloud_account_api.get_blacklist_status("uid", str(uid), region=target_region)
                if status.get("active"):
                    return True
            except Exception as e:
                logger.warning(f"[sekai] failed to check cloud uid blacklist region={target_region} uid={uid}: {e}")
    blacklist = profile_db.get("blacklist", [])
    return str(uid) in [str(item) for item in blacklist]


def check_qid_in_blacklist(qid: int | str) -> bool:
    if is_cloud_account_enabled():
        try:
            status = cloud_account_api.get_blacklist_status("qid", str(qid))
            if status.get("active"):
                return True
        except Exception as e:
            logger.warning(f"[sekai] failed to check cloud qid blacklist qid={qid}: {e}")
    return str(qid) in _get_local_qid_blacklist()


# ======================= 处理逻辑 ======================= #

# 处理敏感指令抓包数据来源
def process_sensitive_cmd_source(data):
    if data.get('source') == 'haruki':
        data['source'] = 'remote'
    if data.get('local_source') == 'haruki':
        data['local_source'] = 'sync'

# 根据游戏id获取玩家基本信息
async def get_basic_profile(ctx: SekaiHandlerContext, uid: int, use_cache=True, use_remote_cache=True, raise_when_no_found=True) -> dict:
    cache_path = f"{SEKAI_PROFILE_DIR}/profile_cache/{ctx.region}/{uid}.json"
    try:
        region_name = get_region_name(ctx.region)
        url = get_gameapi_config(ctx).profile_api_url
        assert_and_reply(url, f"暂不支持查询{region_name}的玩家信息")
        profile = await request_gameapi(url.format(uid=uid) + f"?use_cache={use_remote_cache}")
        if raise_when_no_found:
            assert_and_reply(profile, f"找不到ID为 {uid} 的{region_name}玩家")
        elif not profile:
            return {}
        dump_json(profile, cache_path)
        return profile
    except Exception as e:
        if use_cache and os.path.exists(cache_path):
            logger.print_exc(f"获取 {ctx.region} {uid} 基本信息失败，使用缓存数据")
            profile = load_json(cache_path)
            return profile
        if reply_msg := get_temporary_gameapi_reply("玩家信息服务", e):
            raise ReplyException(reply_msg)
        raise e

# 获取玩家基本信息的简单卡片控件，返回Frame
async def get_basic_profile_card(ctx: SekaiHandlerContext, profile: dict) -> Frame:
    with Frame().set_bg(roundrect_bg()).set_padding(16) as f:
        with HSplit().set_content_align('c').set_item_align('c').set_sep(14):
            avatar_info = await get_player_avatar_info_by_basic_profile(ctx, profile)

            frames = get_player_frames(ctx, profile['user']['userId'], None)
            await get_avatar_widget_with_frame(ctx, avatar_info.img, 80, frames)

            with VSplit().set_content_align('c').set_item_align('l').set_sep(5):
                game_data = profile['user']
                user_id = process_hide_uid(ctx, game_data['userId'], keep=6)
                colored_text_box(
                    truncate(game_data['name'], 64),
                    TextStyle(font=DEFAULT_BOLD_FONT, size=24, color=BLACK, use_shadow=True, shadow_offset=2, shadow_color=ADAPTIVE_SHADOW),
                )
                TextBox(f"{ctx.region.upper()}: {user_id}", TextStyle(font=DEFAULT_FONT, size=16, color=BLACK))
                if 'update_time' in profile:
                    update_time = datetime.fromtimestamp(profile['update_time'] / 1000)
                    update_time_text = update_time.strftime('%m-%d %H:%M:%S') + f" ({get_readable_datetime(update_time, show_original_time=False)})"
                else:
                    update_time_text = "?"
                TextBox(f"更新时间: {update_time_text}", TextStyle(font=DEFAULT_FONT, size=16, color=BLACK))
    return f

# 从玩家基本信息获取该玩家头像PlayerAvatarInfo
async def get_player_avatar_info_by_basic_profile(ctx: SekaiHandlerContext, basic_profile: dict) -> PlayerAvatarInfo:
    decks = basic_profile['userDeck']
    pcards = [find_by(basic_profile['userCards'], 'cardId', decks[f'member{i}']) for i in range(1, 6)]
    for pcard in pcards:
        pcard['after_training'] = pcard['defaultImage'] == "special_training" and pcard['specialTrainingStatus'] == "done"
    card_id = pcards[0]['cardId']
    avatar_img = await get_card_thumbnail(ctx, card_id, pcards[0]['after_training'], high_res=True)
    cid = (await ctx.md.cards.find_by_id(card_id))['characterId']
    unit = await get_unit_by_card_id(ctx, card_id)
    return PlayerAvatarInfo(card_id, cid, unit, avatar_img)

# 查询抓包数据获取模式
def get_user_data_mode(ctx: SekaiHandlerContext, qid: int) -> str:
    if ctx.data_mode_arg:
        assert_and_reply(ctx.data_mode_arg in VALID_DATA_MODES, f"错误的抓包数据获取模式: {ctx.data_mode_arg}")
        return ctx.data_mode_arg
    if state := _get_cloud_state(ctx, qid):
        return state.get("preferences", {}).get("data_mode", DEFAULT_DATA_MODE)
    data_modes = profile_db.get("data_modes", {})
    return data_modes.get(ctx.region, {}).get(str(qid), DEFAULT_DATA_MODE)

# 用户是否隐藏抓包信息
def is_user_hide_suite(ctx: SekaiHandlerContext, qid: int) -> bool:
    if state := _get_cloud_state(ctx, qid):
        return bool(state.get("preferences", {}).get("hide_suite", False))
    hide_list = profile_db.get("hide_suite_list", {}).get(ctx.region, [])
    return qid in hide_list

# 用户是否隐藏id
def is_user_hide_id(region: str, qid: int) -> bool:
    region_ctx = SekaiHandlerContext.from_region(region)
    if state := _get_cloud_state(region_ctx, qid):
        return bool(state.get("preferences", {}).get("hide_id", False))
    hide_list = profile_db.get("hide_id_list", {}).get(region, [])
    return qid in hide_list

# 如果ctx的用户隐藏id则返回隐藏的uid，否则原样返回
def process_hide_uid(ctx: SekaiHandlerContext, uid: int, keep: int=0) -> str:
    if is_user_hide_id(ctx.region, ctx.user_id):
        if keep:
            return "*" * (16 - keep) + str(uid)[-keep:]
        return "*" * 16
    return uid

# 统一Unix时间戳到毫秒，兼容10位秒级与13位毫秒级
def to_unix_millis(ts: int | float | str | None) -> int | None:
    if ts is None or isinstance(ts, bool):
        return None
    if isinstance(ts, str):
        if not ts.isdigit():
            return None
        ts = int(ts)
    if isinstance(ts, (int, float)):
        ts = int(ts)
        if ts < 1_000_000_000_000:
            ts *= 1000
        return ts
    return None

def normalize_upload_time_inplace(data: dict, key: str='upload_time') -> dict:
    if not isinstance(data, dict):
        return data
    ts_ms = to_unix_millis(data.get(key))
    if ts_ms is not None:
        data[key] = ts_ms
    return data

def is_aux_user_data_file(file_path: str) -> bool:
    # 扫描 user_data 时忽略类似 "<uid>.from_suite_api.*.json" 的辅助文件。
    return ".from_suite_api." in os.path.basename(file_path).lower()

def get_uid_from_user_data_file(file_path: str) -> str:
    file_name = os.path.basename(file_path)
    if ".from_suite_api." in file_name:
        return file_name.split(".from_suite_api.", 1)[0]
    return os.path.splitext(file_name)[0]

def load_user_data_json_auto(file_path: str) -> dict:
    with open(file_path, 'rb') as file:
        raw = file.read()
    # zstd 帧头魔数: 28 B5 2F FD
    if len(raw) >= 4 and raw[:4] == b'\x28\xb5\x2f\xfd':
        return load_json_zstd(file_path)
    # 普通 json（兼容带或不带 UTF-8 BOM 的情况）
    try:
        return loads_json(raw)
    except Exception:
        return loads_json(raw.decode('utf-8-sig'))

def get_user_data_local_source(file_path: str) -> str:
    try:
        data = load_user_data_json_auto(file_path)
    except Exception as e:
        logger.warning(f"读取用户抓包文件失败，已跳过: {file_path} ({get_exc_desc(e)})")
        return "读取失败"
    if not isinstance(data, dict):
        logger.warning(f"用户抓包文件不是对象结构，已跳过: {file_path}")
        return "未知"
    return data.get('local_source', '未知')

# 根据获取玩家详细信息，返回(profile, err_msg)
# ================================ Suite 数据获取兼容层 ================================ #
# 这里只按 URL 形态判断接口契约，避免把任何第三方服务域名硬编码进仓库。
def use_suite_public_contract(url: str) -> bool:
    normalized = (url or "").lower()
    return "/public/" in normalized and "{uid}" in normalized


# ================================ 上游临时异常收口 ================================ #
# Public / Profile 旧链路在 5xx 或 HTML/WAF 错误页时，过去会把 HttpError 原样抛到指令层，
# 最终在群里表现成“指令处理失败: HttpError: 502...”。这里集中做一次识别，
# 只收口“明显是上游临时故障”的情况，保留 403/404 等已有业务语义分支。
TEMPORARY_GAMEAPI_STATUS_CODES = {500, 502, 503, 504}


def is_temporary_gameapi_http_error(exc: HttpError) -> bool:
    if exc.status_code in TEMPORARY_GAMEAPI_STATUS_CODES:
        return True
    detail_summary = summarize_http_error_detail(exc.message)
    return "[suspected_html_block_page]" in detail_summary


def is_suspected_html_gameapi_exception(exc: Exception) -> bool:
    exc_text = get_exc_desc(exc).lower()
    return (
        ("unexpected mimetype" in exc_text and "text/html" in exc_text)
        or "<!doctype html" in exc_text
        or "<html" in exc_text
        or "safeline" in exc_text
    )


def get_temporary_gameapi_reply(service_name: str, exc: Exception) -> str | None:
    if isinstance(exc, HttpError):
        if not is_temporary_gameapi_http_error(exc):
            return None
    elif not is_suspected_html_gameapi_exception(exc):
        return None
    return f"{service_name}暂时不可用，请稍后再试"


async def get_detailed_profile(
    ctx: SekaiHandlerContext, 
    qid: int, 
    raise_exc=False, 
    mode=None, 
    ignore_hide=False, 
    filter: list[str] | set[str] | None=None,
    strict: bool=True,
) -> Tuple[dict, str]:
    cache_path = None
    uid = None
    use_suite_api_public = False
    try:
        # 获取绑定的游戏 id
        try:
            uid = get_player_bind_id(ctx)
        except Exception as e:
            logger.info(f"获取 {qid} {ctx.region}抓包数据失败: 未绑定游戏账号")
            raise e
        
        # 检测是否隐藏抓包信息
        if not ignore_hide and is_user_hide_suite(ctx, qid):
            logger.info(f"获取 {qid} {ctx.region} {uid} 抓包数据失败: 用户已隐藏抓包信息")
            raise ReplyException(f"你已隐藏抓包信息，发送\"/{ctx.region}展示抓包\"可重新展示")
        
        # 服务器不支持
        url = get_gameapi_config(ctx).suite_api_url
        if not url:
            raise ReplyException(f"暂不支持查询{get_region_name(ctx.region)}的抓包数据")
        use_suite_api_public = use_suite_public_contract(url)

        # 数据获取模式
        suite_mode = _normalize_suite_source_mode(mode or get_user_suite_source_mode(ctx, qid))
        profile = None
        profile_source_fallback = 'haruki_public'

        # ================================ 优先尝试 OAuth（仅 self-query） ================================ #
        # OAuth 只允许“当前用户查自己的 suite 远端数据”命中。
        # 其余情况（@别人、直接输 UID、管理员代查）统一保持公开接口行为。
        if can_use_oauth_suite(ctx, qid, uid):
            oauth_profile = await get_oauth_game_data(
                str(qid),
                ctx.region,
                'suite',
                str(uid),
                filter=filter,
            )
            if oauth_profile is not None:
                # 只要 OAuth 确实拿到了数据，就把真实 payload 交给下游。
                # 注意这里不能再只接受 dict：
                # - `/抓包状态` 这类单字段查询可能只拿到一个标量 `upload_time`
                # - 如果这里把标量当失败吞掉，就会错误回退到 Public-API
                profile = oauth_profile
                profile_source_fallback = 'haruki_oauth'
            elif suite_mode == 'oauth':
                raise ReplyException("当前抓包模式为 oauth，但 OAuth2 数据暂不可用")

        # ================================ 回退到公开 / 旧后端接口 ================================ #
        # 当前主部署使用的是公开 suite 接口契约；但这里仍保留旧后端的 mode/filter 兼容，
        # 这样其他沿用老链路的实例升级后不会立刻失效。
        if profile is None:
            req_url = url.format(uid=uid)
            try:
                if use_suite_api_public:
                    # suite Public-API 使用 key 参数做字段裁剪。
                    if filter:
                        if isinstance(filter, set):
                            key_list = sorted(filter)
                        else:
                            key_list = list(filter)
                        seen = set()
                        key_list = [k for k in key_list if not (k in seen or seen.add(k))]
                        req_url += f"?key={','.join(key_list)}"
                    profile = await request_gameapi(req_url)
                else:
                    legacy_mode = _get_legacy_suite_request_mode(suite_mode)
                    req_url += f"?mode={legacy_mode}"
                    if filter:
                        req_url += f"&filter={','.join(filter)}"
                    profile = await request_gameapi(req_url)
            except HttpError as e:
                logger.info(f"获取 {qid} {ctx.region} {uid} 抓包数据失败: {get_exc_desc(e)}")

                # suite Public-API 的 key 裁剪并不保证所有字段都支持。
                # 单字段裁剪报 400 时，回退到全量请求比直接判用户失败更稳。
                if use_suite_api_public and e.status_code == 400 and filter:
                    logger.warning(f"suite-api key参数请求失败，回退到全量请求: {req_url}")
                    profile = await request_gameapi(url.format(uid=uid))
                elif use_suite_api_public and e.status_code == 403:
                    raise ReplyException("用户未开启允许公开API访问或无权访问该玩家数据")
                elif e.status_code == 404:
                    detail = e.message if isinstance(e.message, dict) else {}
                    local_err = detail.get('local_err') if isinstance(detail, dict) else None
                    haruki_err = detail.get('haruki_err') if isinstance(detail, dict) else None
                    public_err = detail.get('message') if isinstance(detail, dict) else str(e.message or '').strip()

                    msg = f"获取你的{get_region_name(ctx.region)}Suite抓包数据失败，发送\"/抓包\"指令可获取帮助\n"
                    if local_err is not None:
                        msg += f"[本地数据] {local_err}\n"
                    if haruki_err is not None:
                        msg += f"[Haruki工具箱] {haruki_err}\n"
                    if not local_err and not haruki_err and public_err:
                        msg += f"[Haruki Public-API] {public_err}\n"
                    raise ReplyException(msg.strip())
                else:
                    raise e
            except Exception as e:
                logger.info(f"获取 {qid} {ctx.region} {uid} 抓包数据失败: {get_exc_desc(e)}")
                raise e

        # 某些 suite 后端在只请求单字段时，可能直接返回标量值而不是对象。
        # 这里统一包成 dict，避免下游继续区分两种返回结构。
        if filter and not isinstance(profile, dict):
            filter_keys = sorted(filter) if isinstance(filter, set) else list(filter)
            if len(filter_keys) == 1:
                profile = {filter_keys[0]: profile}

        if not profile:
            logger.info(f"获取 {qid} {ctx.region} {uid} 抓包数据失败: 找不到该玩家")
            raise ReplyException(f"找不到ID为 {uid} 的玩家")

        profile['source'] = normalize_suite_source_value(profile.get('source'), profile_source_fallback)
        # 记录本次查询目标，供展示层判断“是否属于自己查询”使用。
        # 这两个字段只在 Bot 进程内流转，不作为对外契约依赖。
        profile['_requested_qid'] = str(qid)
        profile['_requested_uid'] = str(uid)

        # 统一 upload_time 单位为毫秒（兼容秒级/毫秒级）
        normalize_upload_time_inplace(profile)
        
        # 默认关闭suite本地缓存（保留注释便于按需恢复）
        cache_path = f"{SEKAI_PROFILE_DIR}/suite_cache/{ctx.region}/{uid}.json"
        # if use_cache:
        #     dump_json(profile, cache_path)
        logger.info(f"获取 {qid} {ctx.region} {uid} 抓包数据成功")
        
    except Exception as e:
        # 获取失败的情况，尝试读取缓存
        if cache_path and os.path.exists(cache_path):
            profile = load_json(cache_path)
            profile['source'] = normalize_suite_source_value(profile.get('source'), 'haruki_public')
            profile['_requested_qid'] = str(qid)
            profile['_requested_uid'] = str(uid) if uid else str(profile.get('_requested_uid') or '')
            normalize_upload_time_inplace(profile)
            logger.info(f"从缓存获取 {qid} {ctx.region} {uid} 抓包数据")
            return profile, get_exc_desc(e) + "(使用先前的缓存数据)"
        else:
            logger.info(f"未找到 {qid} {ctx.region} {uid} 的缓存抓包数据")

        if reply_msg := get_temporary_gameapi_reply(
            "Haruki Public-API" if use_suite_api_public else "抓包数据服务",
            e,
        ):
            if raise_exc:
                raise ReplyException(reply_msg)
            return None, reply_msg

        if raise_exc:
            raise e
        else:
            return None, get_exc_desc(e)

    if strict and filter:
        missing_keys = [k for k in filter if k not in profile]
        if missing_keys:
            raise ReplyException(format_suite_missing_fields_error(ctx, profile, missing_keys))
        
    return profile, ""

# 获取包含了玩家详细信息的简单卡片控件所需要的filter
def get_detailed_profile_card_filter(*s: str) -> set[str]:
    return {'userGamedata', 'userDecks', 'upload_time', 'userCards', *s}

# 从玩家详细信息获取该玩家头像的PlayerAvatarInfo
async def get_player_avatar_info_by_detailed_profile(ctx: SekaiHandlerContext, detail_profile: dict) -> PlayerAvatarInfo:
    _, deck = _get_avatar_deck_from_detailed_profile(ctx, detail_profile)

    member_card_ids = [deck.get(f'member{i}') for i in range(1, 6)]
    if any(card_id in (None, '') for card_id in member_card_ids):
        raise ReplyException(format_suite_missing_fields_error(ctx, detail_profile, ['userDecks']))

    pcards = [find_by(detail_profile['userCards'], 'cardId', card_id) for card_id in member_card_ids]
    if any(not pcard for pcard in pcards):
        raise ReplyException(format_suite_missing_fields_error(ctx, detail_profile, ['userCards']))

    for pcard in pcards:
        pcard['after_training'] = pcard['defaultImage'] == "special_training" and pcard['specialTrainingStatus'] == "done"
    card_id = pcards[0]['cardId']
    avatar_img = await get_card_thumbnail(ctx, card_id, pcards[0]['after_training'], high_res=True)
    cid = (await ctx.md.cards.find_by_id(card_id))['characterId']
    unit = await get_unit_by_card_id(ctx, card_id)
    return PlayerAvatarInfo(card_id, cid, unit, avatar_img)

# 获取玩家详细信息的简单卡片控件，返回Frame
async def get_detailed_profile_card(ctx: SekaiHandlerContext, profile: dict, err_msg: str, mode=None) -> Frame:
    with Frame().set_bg(roundrect_bg()).set_padding(16) as f:
        with HSplit().set_content_align('c').set_item_align('c').set_sep(14):
            if profile:
                avatar_info = await get_player_avatar_info_by_detailed_profile(ctx, profile)

                frames = get_player_frames(ctx, profile['userGamedata']['userId'], profile)
                await get_avatar_widget_with_frame(ctx, avatar_info.img, 80, frames)

                with VSplit().set_content_align('c').set_item_align('l').set_sep(5):
                    game_data = profile['userGamedata']
                    source = get_suite_source_display_name(profile.get('source', '?'))
                    if local_source := profile.get('local_source'):
                        source += f"({local_source})"
                    mode = _normalize_suite_source_mode(mode or get_user_suite_source_mode(ctx, ctx.user_id))
                    update_time = datetime.fromtimestamp(profile['upload_time'] / 1000)
                    update_time_text = update_time.strftime('%m-%d %H:%M:%S') + f" ({get_readable_datetime(update_time, show_original_time=False)})"
                    user_id = process_hide_uid(ctx, game_data['userId'], keep=6)
                    colored_text_box(
                        truncate(_get_suite_display_name_for_card(ctx, profile), 64),
                        TextStyle(font=DEFAULT_BOLD_FONT, size=24, color=BLACK, use_shadow=True, shadow_offset=2),
                    )
                    TextBox(f"{ctx.region.upper()}: {user_id} Suite数据", TextStyle(font=DEFAULT_FONT, size=16, color=BLACK))
                    TextBox(f"更新时间: {update_time_text}", TextStyle(font=DEFAULT_FONT, size=16, color=BLACK))
                    TextBox(f"数据来源: {source}  获取模式: {mode}", TextStyle(font=DEFAULT_FONT, size=16, color=BLACK))
                    if warning_text := get_suite_public_warning_text(ctx, profile):
                        TextBox(warning_text, TextStyle(font=DEFAULT_FONT, size=16, color=RED))
            if err_msg:
                TextBox(f"获取数据失败: {err_msg}", TextStyle(font=DEFAULT_FONT, size=20, color=RED), line_count=3).set_w(300)
    return f

# 获取注册时间，无效uid返回None
def get_register_time(region: str, uid: str) -> datetime:
    try:
        if region in ['jp', 'en']:
            time = int(uid[:-3]) / 1024 / 4096
            return datetime.fromtimestamp(1600218000 + int(time))
        elif region in ['tw', 'cn', 'kr']:
            time = int(uid) / 1024 / 1024 / 4096
            return datetime.fromtimestamp(int(time))
    except ValueError:
        return None

# 合成个人信息图片
async def compose_profile_image(ctx: SekaiHandlerContext, basic_profile: dict, vertical: bool=None) -> Image.Image:
    bg_settings = get_profile_bg_settings(ctx)
    detail_profile, _ = await get_detailed_profile(
        ctx, ctx.user_id, raise_exc=False, ignore_hide=True, 
        filter=['upload_time', 'userPlayerFrames'],
        strict=False,
    )
    uid = str(basic_profile['user']['userId'])

    decks = basic_profile['userDeck']
    pcards = [find_by(basic_profile['userCards'], 'cardId', decks[f'member{i}']) for i in range(1, 6)]
    for pcard in pcards:
        pcard['after_training'] = pcard['defaultImage'] == "special_training" and pcard['specialTrainingStatus'] == "done"
    avatar_info = await get_player_avatar_info_by_basic_profile(ctx, basic_profile)

    bg = ImageBg(bg_settings.image, blur=False, fade=0) if bg_settings.image else random_unit_bg(avatar_info.unit)
    ui_bg = roundrect_bg(fill=(255, 255, 255, bg_settings.alpha), blurglass=True, blurglass_kwargs={'blur': bg_settings.blur})

    async def draw_honor():
        with HSplit().set_content_align('c').set_item_align('c').set_sep(8).set_padding((16, 0)):
            honors = basic_profile["userProfileHonors"]
            async def compose_honor_image_nothrow(*args):
                try: return await compose_full_honor_image(*args)
                except: 
                    logger.print_exc("合成头衔图片失败")
                    return None
            honor_imgs = await asyncio.gather(*[
                compose_honor_image_nothrow(ctx, find_by(honors, 'seq', 1), True, basic_profile),
                compose_honor_image_nothrow(ctx, find_by(honors, 'seq', 2), False, basic_profile),
                compose_honor_image_nothrow(ctx, find_by(honors, 'seq', 3), False, basic_profile)
            ])
            for img in honor_imgs:
                if img: 
                    ImageBox(img, size=(None, 48), shadow=True)

    async def draw_deck(vertical: bool):
        with HSplit().set_content_align('c').set_item_align('c').set_sep(6 if not vertical else 16).set_padding((16, 0)):
            card_ids = [pcard['cardId'] for pcard in pcards]
            cards = await ctx.md.cards.collect_by_ids(card_ids)
            card_imgs = [
                await get_card_full_thumbnail(ctx, card, pcard=pcard, high_res=True)
                for card, pcard in zip(cards, pcards)
            ]
            for i in range(len(card_imgs)):
                ImageBox(card_imgs[i], size=(90, 90), image_size_mode='fill', shadow=True)

    # 个人信息部分
    async def draw_info(vertical: bool): 
        with VSplit().set_bg(ui_bg).set_content_align('c').set_item_align('c').set_sep(32).set_padding((32, 35)) as ret:
            # 名片
            with HSplit().set_content_align('c').set_item_align('c').set_sep(32).set_padding((32, 0)):
                frames = get_player_frames(ctx, uid, detail_profile)
                await get_avatar_widget_with_frame(ctx, avatar_info.img, 128, frames)

                with VSplit().set_content_align('c').set_item_align('l').set_sep(16):
                    game_data = basic_profile['user']
                    colored_text_box(
                        truncate(game_data['name'], 64),
                        TextStyle(font=DEFAULT_BOLD_FONT, size=32, color=ADAPTIVE_WB, use_shadow=True, shadow_offset=2),
                    )
                    TextBox(f"{ctx.region.upper()}: {process_hide_uid(ctx, game_data['userId'], keep=6)}", TextStyle(font=DEFAULT_FONT, size=20, color=ADAPTIVE_WB))
                    with Frame():
                        ImageBox(ctx.static_imgs.get("lv_rank_bg.png"), size=(180, None))
                        TextBox(f"{game_data['rank']}", TextStyle(font=DEFAULT_FONT, size=30, color=WHITE)).set_offset((110, 7))\
                        
            # 头衔（竖版）
            if vertical:
                await draw_honor()

            # 推特
            with Frame().set_content_align('l').set_w(450):
                tw_id = basic_profile['userProfile'].get('twitterId', '')
                tw_id_box = TextBox('        @ ' + tw_id, TextStyle(font=DEFAULT_FONT, size=20, color=ADAPTIVE_WB), line_count=1)
                tw_id_box.set_wrap(False).set_bg(ui_bg).set_line_sep(2).set_padding(10).set_w(300).set_content_align('l')
                x_icon = ctx.static_imgs.get("x_icon.png").resize((24, 24)).convert('RGBA')
                ImageBox(x_icon, image_size_mode='original').set_offset((16, 0))

            # 留言
            user_word = basic_profile['userProfile'].get('word', '')
            user_word = re.sub(r'<#.*?>', '', user_word)
            user_word_box = TextBox(user_word, TextStyle(font=DEFAULT_FONT, size=20, color=ADAPTIVE_WB), line_count=3)
            user_word_box.set_wrap(True).set_bg(ui_bg).set_line_sep(2).set_padding((18, 16)).set_w(450)

            # 头衔（横版）
            if not vertical:
                await draw_honor()
            
            # 卡组（横版）
            if not vertical:
                await draw_deck(vertical)
            
        return ret

    # 打歌部分
    async def draw_play(vertical: bool): 
        with HSplit().set_content_align('c').set_item_align('t').set_sep(12).set_bg(ui_bg).set_padding(32) as ret:
            hs, vs, gw, gh = 8, 12, 90, 25
            with VSplit().set_sep(vs):
                Spacer(gh, gh)
                ImageBox(ctx.static_imgs.get(f"icon_clear.png"), size=(gh, gh))
                ImageBox(ctx.static_imgs.get(f"icon_fc.png"), size=(gh, gh))
                ImageBox(ctx.static_imgs.get(f"icon_ap.png"), size=(gh, gh))
            with Grid(col_count=6).set_sep(hsep=hs, vsep=vs):
                for diff, color in DIFF_COLORS.items():
                    t = TextBox(diff.upper(), TextStyle(font=DEFAULT_BOLD_FONT, size=16, color=WHITE))
                    t.set_bg(RoundRectBg(fill=color, radius=3)).set_size((gw, gh)).set_content_align('c')
                diff_count = basic_profile['userMusicDifficultyClearCount']
                scores = ['liveClear', 'fullCombo', 'allPerfect']
                play_result = ['clear', 'fc', 'ap']
                for i, score in enumerate(scores):
                    for j, diff in enumerate(DIFF_COLORS.keys()):
                        bg_color = (255, 255, 255, 150) if j % 2 == 0 else (255, 255, 255, 100)
                        count = find_by(diff_count, 'musicDifficultyType', diff)[score]
                        TextBox(str(count), TextStyle(
                                DEFAULT_FONT, 20, PLAY_RESULT_COLORS['not_clear'], use_shadow=True,
                                shadow_color=PLAY_RESULT_COLORS[play_result[i]], shadow_offset=1,
                            )).set_bg(RoundRectBg(fill=bg_color, radius=3)).set_size((gw, gh)).set_content_align('c')
        return ret
    
    # 养成部分
    async def draw_chara(vertical: bool):
        with VSplit().set_sep(16).set_item_bg(ui_bg) as ret:
            with Frame().set_content_align('rb'):
                hs, vs, gw, gh = 8, 7, 96, 48
                # 角色等级
                with Grid(col_count=6).set_sep(hsep=hs, vsep=vs).set_padding(32):
                    chara_list = [
                        "miku", "rin", "len", "luka", "meiko", "kaito", 
                        "ick", "saki", "hnm", "shiho", None, None,
                        "mnr", "hrk", "airi", "szk", None, None,
                        "khn", "an", "akt", "toya", None, None,
                        "tks", "emu", "nene", "rui", None, None,
                        "knd", "mfy", "ena", "mzk", None, None,
                    ]
                    for chara in chara_list:
                        if chara is None:
                            Spacer(gw, gh)
                            continue
                        cid = int(get_cid_by_nickname(chara))
                        rank = find_by(basic_profile['userCharacters'], 'characterId', cid)['characterRank']
                        with Frame().set_size((gw, gh)):
                            chara_img = ctx.static_imgs.get(f'chara_rank_icon/{chara}.png')
                            ImageBox(chara_img, size=(gw, gh), use_alphablend=True)
                            t = TextBox(str(rank), TextStyle(font=DEFAULT_FONT, size=20, color=(40, 40, 40, 255)))
                            t.set_size((60, 48)).set_content_align('c').set_offset((34, 0))
                
                # 挑战Live等级
                if 'userChallengeLiveSoloResult' in basic_profile:
                    solo_live_result = basic_profile['userChallengeLiveSoloResult']
                    if isinstance(solo_live_result, list):
                        solo_live_result = sorted(solo_live_result, key=lambda x: x['highScore'], reverse=True)[0]
                    cid, score = solo_live_result['characterId'], solo_live_result['highScore']
                    stages = find_by(basic_profile['userChallengeLiveSoloStages'], 'characterId', cid, mode='all')
                    stage_rank = max([stage['rank'] for stage in stages])
                    
                    with VSplit().set_content_align('c').set_item_align('c').set_padding((32, 64)).set_sep(12):
                        t = TextBox(f"CHANLLENGE LIVE", TextStyle(font=DEFAULT_FONT, size=18, color=(50, 50, 50, 255)))
                        t.set_bg(roundrect_bg(radius=6)).set_padding((10, 7))
                        with Frame():
                            chara_img = ctx.static_imgs.get(f'chara_rank_icon/{get_character_first_nickname(cid)}.png')
                            ImageBox(chara_img, size=(100, 50), use_alphablend=True)
                            t = TextBox(str(stage_rank), TextStyle(font=DEFAULT_FONT, size=22, color=(40, 40, 40, 255)), overflow='clip')
                            t.set_size((50, 50)).set_content_align('c').set_offset((40, 0))
                        t = TextBox(f"SCORE {score}", TextStyle(font=DEFAULT_FONT, size=18, color=(50, 50, 50, 255)))
                        t.set_bg(roundrect_bg(radius=6)).set_padding((10, 7))

            # 卡组（竖版）
            if vertical:
                with Frame().set_content_align('c').set_padding(32):
                    await draw_deck(vertical)
        return ret

    if vertical is None:
        vertical = bg_settings.vertical

    with Canvas(bg=bg).set_padding(BG_PADDING) as canvas:
        if not vertical:
            with HSplit().set_content_align('lt').set_item_align('lt').set_sep(16):
                await draw_info(vertical)
                with VSplit().set_content_align('c').set_item_align('c').set_sep(16):
                    await draw_play(vertical)
                    await draw_chara(vertical)
        else:
            with VSplit().set_content_align('c').set_item_align('c').set_sep(16).set_item_bg(ui_bg):
                (await draw_info(vertical)).set_bg(None)
                (await draw_play(vertical)).set_bg(None)
                (await draw_chara(vertical)).set_bg(None).set_omit_parent_bg(True)

    update_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    text = f"DT: {update_time}  " + DEFAULT_WATERMARK_CFG.get()
    if bg_settings.image:
        text = text + f"  This background is user-uploaded."
    add_watermark(canvas, text)
    return await canvas.get_img(1.5)

# 个人信息背景设置
async def set_profile_bg_settings(
    ctx: SekaiHandlerContext,
    image: Optional[Image.Image] = None,
    remove_image: bool = False,
    blur: Optional[int] = None,
    alpha: Optional[int] = None,
    vertical: Optional[bool] = None,
    force: bool = False
):
    uid = await get_uid_and_check_verified(ctx, force)
    region = ctx.region
    image_path = PROFILE_BG_IMAGE_PATH.format(region=region, uid=uid)

    settings: Dict[str, Dict[str, Any]] = profile_bg_settings_db.get(region, {})
    
    if remove_image:
        if os.path.exists(image_path):
            os.remove(image_path)
    elif image:
        w, h = image.size
        w1, h1 = config.get('profile.bg_image_size.horizontal')
        w2, h2 = config.get('profile.bg_image_size.vertical')
        scale = -1
        if w > w1 and h > h1:
            scale = max(scale, w1 / w, h1 / h)
        if w > w2 and h > h2:
            scale = max(scale, w2 / w, h2 / h)
        if scale < 0:
            scale = 1
        target_w, target_h = int(w * scale), int(h * scale)
        assert_and_reply(min(target_w, target_h) < 10000, "上传图片的横纵比过大或过小")
        image = image.convert('RGB')
        if image.width > target_w:
            image = image.resize((target_w, target_h), Image.LANCZOS)
        save_kwargs = config.get('profile.bg_image_save_kwargs', {})
        create_parent_folder(image_path)
        image.save(image_path, **save_kwargs)
        settings.setdefault(uid, {})['vertical'] = target_w < target_h
        if 'blur' not in settings.get(uid, {}):
            settings.setdefault(uid, {})['blur'] = 1
        if 'alpha' not in settings.get(uid, {}):
            settings.setdefault(uid, {})['alpha'] = 50

    if blur is not None:
        blur = max(0, min(10, blur))
        settings.setdefault(uid, {})['blur'] = blur

    if alpha is not None:
        alpha = max(0, min(255, alpha))
        settings.setdefault(uid, {})['alpha'] = alpha

    if vertical is not None:
        settings.setdefault(uid, {})['vertical'] = vertical

    profile_bg_settings_db.set(region, settings)

# 个人信息背景设置获取
def get_profile_bg_settings(ctx: SekaiHandlerContext) -> ProfileBgSettings:
    uid = get_player_bind_id(ctx)
    region = ctx.region
    try:
        image = open_image(PROFILE_BG_IMAGE_PATH.format(region=region, uid=uid))
    except:
        image = None
    settings = profile_bg_settings_db.get(region, {}).get(uid, {})
    ret = ProfileBgSettings(image=image, **settings)
    if ret.alpha is None:
        ret.alpha = WIDGET_BG_COLOR_CFG.get()[3]
    if ret.blur is None:
        ret.blur = 4
    return ret

# 获取玩家框信息，提供detail_profile会直接取用并更新缓存，否则使用缓存数据
def get_player_frames(ctx: SekaiHandlerContext, uid: str, detail_profile: Optional[dict] = None) -> List[dict]:
    uid = str(uid)
    all_cached_frames = player_frame_db.get(ctx.region, {})
    cached_frames = all_cached_frames.get(uid, {})
    if detail_profile:
        upload_time = detail_profile.get('upload_time', 0)
        frames = detail_profile.get('userPlayerFrames', [])
        if upload_time > cached_frames.get('upload_time', 0):
            # 更新缓存
            cached_frames = {
                'upload_time': upload_time,
                'frames': frames
            }
            if frames:
                all_cached_frames[uid] = cached_frames
                player_frame_db.set(ctx.region, all_cached_frames)
    return cached_frames.get('frames', [])

# 获取头像框图片，失败返回None
async def get_player_frame_image(ctx: SekaiHandlerContext, frame_id: int, frame_w: int) -> Image.Image | None:
    try:
        frame = await ctx.md.player_frames.find_by_id(frame_id)
        frame_group = await ctx.md.player_frame_groups.find_by_id(frame['playerFrameGroupId'])
        asset_name = frame_group['assetbundleName']
        asset_path = f"player_frame/{asset_name}/{frame_id}/"

        cache_path = f"{SEKAI_ASSET_DIR}/player_frames/{ctx.region}/{asset_name}_{frame_id}.png"

        scale = 1.5
        corner = 20
        corner2 = 50
        w = 700
        border = 100
        border2 = 80
        inner_w = w - 2*border

        if os.path.exists(cache_path):
            img = open_image(cache_path)
        else:
            base = await ctx.rip.img(asset_path + "horizontal/frame_base.png", allow_error=False)
            ct = await ctx.rip.img(asset_path + "vertical/frame_centertop.png", allow_error=False)
            lb = await ctx.rip.img(asset_path + "vertical/frame_leftbottom.png", allow_error=False)
            lt = await ctx.rip.img(asset_path + "vertical/frame_lefttop.png", allow_error=False)
            rb = await ctx.rip.img(asset_path + "vertical/frame_rightbottom.png", allow_error=False)
            rt = await ctx.rip.img(asset_path + "vertical/frame_righttop.png", allow_error=False)

            try:
                ct = (await run_in_pool(shrink_image, ct, 10, 0)).image
            except Exception as e:
                logger.warning(f"合成playerFrame_{frame_id}时为ct执行shrink失败（可能导致错位）: {get_exc_desc(e)}")
            
            ct = resize_keep_ratio(ct, scale, mode='scale')
            lt = resize_keep_ratio(lt, scale, mode='scale')
            lb = resize_keep_ratio(lb, scale, mode='scale')
            rt = resize_keep_ratio(rt, scale, mode='scale')
            rb = resize_keep_ratio(rb, scale, mode='scale')

            bw = base.width
            base_lt = base.crop((0, 0, corner, corner))
            base_rt = base.crop((bw-corner, 0, bw, corner))
            base_lb = base.crop((0, bw-corner, corner, bw))
            base_rb = base.crop((bw-corner, bw-corner, bw, bw))
            base_l = base.crop((0, corner, corner, bw-corner))
            base_r = base.crop((bw-corner, corner, bw, bw-corner))
            base_t = base.crop((corner, 0, bw-corner, corner))
            base_b = base.crop((corner, bw-corner, bw-corner, bw))

            p = Painter(size=(w, w))

            p.move_region((border, border), (inner_w, inner_w))
            p.paste(base_lt, (0, 0), (corner2, corner2))
            p.paste(base_rt, (inner_w-corner2, 0), (corner2, corner2))
            p.paste(base_lb, (0, inner_w-corner2), (corner2, corner2))
            p.paste(base_rb, (inner_w-corner2, inner_w-corner2), (corner2, corner2))
            p.paste(base_l.resize((corner2, inner_w-2*corner2)), (0, corner2))
            p.paste(base_r.resize((corner2, inner_w-2*corner2)), (inner_w-corner2, corner2))
            p.paste(base_t.resize((inner_w-2*corner2, corner2)), (corner2, 0))
            p.paste(base_b.resize((inner_w-2*corner2, corner2)), (corner2, inner_w-corner2))
            p.restore_region()

            p.paste(lb, (border2, w-border2-lb.height))
            p.paste(rb, (w-border2-rb.width, w-border2-rb.height))
            p.paste(lt, (border2, border2))
            p.paste(rt, (w-border2-rt.width, border2))
            p.paste(ct, ((w-ct.width)//2, border2-ct.height//2))

            img = await p.get()
            create_parent_folder(cache_path)
            img.save(cache_path)

        img = resize_keep_ratio(img, frame_w / inner_w, mode='scale')
        return img

    except:
        logger.print_exc(f"获取playerFrame {frame_id} 失败")
        return None
    
# 获取带框头像控件
async def get_avatar_widget_with_frame(ctx: SekaiHandlerContext, avatar_img: Image.Image, avatar_w: int, frame_data: list[dict]) -> Frame:
    frame_img = None
    try:
        if frame := find_by(frame_data, 'playerFrameAttachStatus', "first"):
            frame_img = await get_player_frame_image(ctx, frame['playerFrameId'], avatar_w + 5)
    except:
        pass

    # 期间限定框
    term_limit_frame_img: Image.Image = None
    try:
        for limited_time_frame in config.get('profile.limited_time_custom_frames', []):
            now = datetime.now()
            for period in limited_time_frame.get('periods', []):
                start = datetime.strptime(period[0], '%m-%d %H:%M').replace(year=now.year)
                end = datetime.strptime(period[1], '%m-%d %H:%M').replace(year=now.year)
                if start <= now <= end:
                    term_limit_frame_img = ctx.static_imgs.get(limited_time_frame['path'])
                    term_limit_frame_img = resize_keep_ratio(term_limit_frame_img, avatar_w, scale=limited_time_frame.get('scale', 1.0))
                    break
            if term_limit_frame_img:
                break
    except Exception as e:
        logger.warning(f"获取期间限定头像框失败: {get_exc_desc(e)}")
        term_limit_frame_img = None

    with Frame().set_size((avatar_w, avatar_w)).set_content_align('c').set_allow_draw_outside(True) as ret:
        ImageBox(avatar_img, size=(avatar_w, avatar_w), use_alphablend=False, shadow=True)
        if frame_img:
            ImageBox(frame_img, use_alphablend=True, shadow=True)
        if term_limit_frame_img:
            ImageBox(term_limit_frame_img, use_alphablend=True, shadow=True)
    return ret

# ======================= 角色等级任务总览 ======================= #

CHAR_MISSION_SHORT_NAMES = {
    "play_live": "队长次数",
    "play_live_ex": "队长次数(EX)",
    "waiting_room": "休息室次数",
    "waiting_room_ex": "休息室次数(EX)",
    "collect_costume_3d": "服装",
    "collect_stamp": "表情",
    "read_area_talk": "区域对话",
    "read_card_episode_first": "卡面剧情前篇",
    "read_card_episode_second": "卡面剧情后篇",
    "collect_another_vocal": "Another Vocal",
    "area_item_level_up_character": "单人家具升级次数",
    "area_item_level_up_unit": "团家具升级次数",
    "area_item_level_up_reality_world": "属性道具（树&花）升级次数",
    "collect_member": "卡面",
    "skill_level_up_rare": "技能等级升级次数（★4&生日卡）",
    "skill_level_up_standard": "技能等级升级次数（★1~★3）",
    "master_rank_up_rare": "专精等级升级次数（★4&生日卡）",
    "master_rank_up_standard": "专精等级升级次数（★1~★3）",
    "collect_character_archive_voice": "台词",
    "collect_mysekai_fixture": "MySekai家具数量",
    "collect_mysekai_canvas": "MySekai画布数量",
    "read_mysekai_fixture_unique_character_talk": "MySekai对话",
}

CHAR_MISSION_EX_TYPES = {"play_live_ex", "waiting_room_ex"}
CHAR_MISSION_EX_BASE_TYPES = {"play_live", "waiting_room"}


def _char_mission_short_name(mission_type: str) -> str:
    return CHAR_MISSION_SHORT_NAMES.get(mission_type, mission_type)


def _get_pg_requirement_by_seq(
    pg_seq_requirements: dict[int, list[tuple[int, int]]],
    parameter_group_id: int,
    seq: int,
) -> int:
    if seq <= 0:
        return 0
    req = 0
    for item_seq, item_req in pg_seq_requirements.get(parameter_group_id, []):
        if item_seq > seq:
            break
        req = item_req
    return req


def _calc_mission_percent(current: int, upper: int | None) -> str:
    if upper is None or upper <= 0:
        return "-"
    return f"{min(current / upper * 100, 100.0):.1f}%"


def _draw_single_progress(
    line_title: str,
    current: int,
    upper: int | None,
    ratio: float,
    bar_width: int,
    bar_color: Color,
    title_size: int = 16,
    title_align: str = "l",
    title_badge: str | None = None,
    next_need: int | None = None,
    next_exp: int | None = None,
):
    style_title = TextStyle(font=DEFAULT_BOLD_FONT, size=title_size, color=(35, 35, 35, 255))
    style_text = TextStyle(font=DEFAULT_FONT, size=15, color=(55, 55, 55, 255))

    if line_title:
        if title_badge:
            with Frame().set_w(bar_width).set_content_align(title_align):
                with HSplit().set_content_align(title_align).set_item_align('c').set_sep(8):
                    TextBox(line_title, style_title)
                    TextBox(title_badge, TextStyle(font=DEFAULT_BOLD_FONT, size=16, color=(55, 55, 55, 255))) \
                        .set_bg(roundrect_bg(fill=(255, 255, 255, 180), radius=8)).set_padding((8, 2))
        else:
            TextBox(line_title, style_title).set_w(bar_width).set_content_align(title_align)
        Spacer(w=bar_width, h=4)

    raw_ratio = ratio
    if upper is not None and upper > 0:
        raw_ratio = current / upper
    final_bar_color = (255, 50, 50, 255)
    if raw_ratio >= 1.0:
        final_bar_color = (100, 255, 100, 255)
    elif raw_ratio > 0.8:
        final_bar_color = (255, 255, 100, 255)
    elif raw_ratio > 0.6:
        final_bar_color = (255, 200, 100, 255)
    elif raw_ratio > 0.4:
        final_bar_color = (255, 150, 100, 255)
    elif raw_ratio > 0.2:
        final_bar_color = (255, 100, 100, 255)

    with Frame().set_w(bar_width).set_h(18).set_content_align('lt'):
        progress = max(0.0, min(ratio, 1.0))
        total_w, total_h, border = bar_width, 14, 2
        progress_w = int((total_w - border * 2) * progress)
        progress_h = total_h - border * 2

        if progress > 0:
            Spacer(w=total_w, h=total_h).set_bg(RoundRectBg(fill=(100, 100, 100, 255), radius=total_h // 2))
            Spacer(w=progress_w, h=progress_h).set_bg(
                RoundRectBg(fill=final_bar_color, radius=(total_h - border) // 2)
            ).set_offset((border, border))

            for i in range(1, 5):
                lx = int((total_w - border * 2) * (i / 5.0))
                line_color = (100, 100, 100, 255) if i / 5.0 < progress else (150, 150, 150, 255)
                Spacer(w=1, h=total_h // 2 - 1).set_bg(FillBg(line_color)).set_offset((border + lx - 1, total_h // 2))
        else:
            Spacer(w=total_w, h=total_h).set_bg(RoundRectBg(fill=(100, 100, 100, 100), radius=total_h // 2))

    upper_text = "∞" if upper is None else f"{upper:,}"
    pct_text = _calc_mission_percent(current, upper)
    with HSplit().set_content_align('c').set_item_align('c').set_sep(8):
        TextBox(f"{current:,}/{upper_text} ({pct_text})", style_text).set_content_align('l')
        if next_need is not None:
            exp_text = "?" if next_exp is None else str(next_exp)
            TextBox(
                f"下一档{current:,}/{next_need:,} EXP+{exp_text}",
                TextStyle(font=DEFAULT_FONT, size=14, color=(80, 80, 80, 255)),
            ).set_content_align('r')
        else:
            TextBox(
                "下一档已满",
                TextStyle(font=DEFAULT_FONT, size=14, color=(80, 80, 80, 255)),
            ).set_content_align('r')


def _build_single_mission_card(
    title: str,
    current: int,
    upper: int | None,
    ratio: float,
    card_w: int,
    bar_color: Color = (82, 165, 255, 255),
    next_need: int | None = None,
    next_exp: int | None = None,
) -> Frame:
    with Frame().set_w(card_w).set_bg(roundrect_bg(fill=(255, 255, 255, 140))).set_padding((12, 10)) as card:
        with VSplit().set_content_align('l').set_item_align('l').set_sep(10):
            _draw_single_progress(
                title,
                current,
                upper,
                ratio,
                bar_width=card_w - 24,
                bar_color=bar_color,
                title_size=20,
                title_align='c',
                next_need=next_need,
                next_exp=next_exp,
            )
    return card


def _build_dual_mission_card(
    title: str,
    normal_current: int,
    normal_upper: int | None,
    normal_ratio: float,
    normal_next_need: int | None,
    normal_next_exp: int | None,
    ex_current: int,
    ex_upper: int | None,
    ex_ratio: float,
    ex_next_need: int | None,
    ex_next_exp: int | None,
    card_w: int,
    ex_round_text: str,
) -> Frame:
    with Frame().set_w(card_w).set_bg(roundrect_bg(fill=(255, 255, 255, 155))).set_padding((12, 10)) as card:
        with VSplit().set_content_align('l').set_item_align('l').set_sep(10):
            with Frame().set_w(card_w - 24).set_content_align('c'):
                with HSplit().set_content_align('c').set_item_align('c').set_sep(8):
                    TextBox(title, TextStyle(font=DEFAULT_BOLD_FONT, size=20, color=(20, 20, 20, 255))).set_content_align('c')
            _draw_single_progress(
                "普通任务",
                normal_current,
                normal_upper,
                normal_ratio,
                bar_width=card_w - 24,
                bar_color=(84, 170, 255, 255),
                title_align='c',
                next_need=normal_next_need,
                next_exp=normal_next_exp,
            )
            _draw_single_progress(
                "EX任务",
                ex_current,
                ex_upper,
                ex_ratio,
                bar_width=card_w - 24,
                bar_color=(255, 145, 84, 255),
                title_align='c',
                title_badge=ex_round_text,
                next_need=ex_next_need,
                next_exp=ex_next_exp,
            )
    return card


async def compose_character_rank_mission_overview_image(
    ctx: SekaiHandlerContext,
    profile: dict,
    err_msg: str,
    cid: int,
) -> Image.Image:
    async def get_masterdata_with_local_fallback(name: str):
        try:
            return await ctx.md.get(name)
        except Exception as e:
            local_path = pjoin(MASTER_DB_CACHE_DIR, ctx.region, f"{name}.json")
            if os.path.exists(local_path):
                logger.warning(
                    f"获取 MasterData [{ctx.region}.{name}] 失败，回退到本地文件: {get_exc_desc(e)}"
                )
                return load_json(local_path)
            raise e

    master_missions = await get_masterdata_with_local_fallback("characterMissionV2s")
    parameter_groups = await get_masterdata_with_local_fallback("characterMissionV2ParameterGroups")

    pg_seq_requirements: dict[int, list[tuple[int, int]]] = {}
    pg_seq_req_exp: dict[int, list[tuple[int, int, int]]] = {}
    pg_max_requirement: dict[int, int] = {}
    pg_seq_exp: dict[tuple[int, int], int] = {}
    for item in parameter_groups:
        pgid = item["id"]
        pg_seq_requirements.setdefault(pgid, []).append((item["seq"], item["requirement"]))
        pg_seq_req_exp.setdefault(pgid, []).append((item["seq"], item["requirement"], int(item.get("exp", 0))))
        pg_max_requirement[pgid] = max(pg_max_requirement.get(pgid, 0), item["requirement"])
        pg_seq_exp[(pgid, item["seq"])] = int(item.get("exp", 0))
    for items in pg_seq_requirements.values():
        items.sort(key=lambda x: x[0])
    for items in pg_seq_req_exp.values():
        items.sort(key=lambda x: x[0])

    def get_ex_round_requirement(pgid: int, round_no: int) -> int:
        req = 0
        for seq, requirement in pg_seq_requirements.get(pgid, []):
            if seq > round_no:
                break
            req = requirement
        return req

    def get_ex_round_exp(pgid: int, round_no: int) -> int:
        exp = 0
        for seq, _, seq_exp in pg_seq_req_exp.get(pgid, []):
            if seq > round_no:
                break
            exp = seq_exp
        return exp

    def calc_ex_round_and_progress(total: int, pgid: int) -> tuple[int, int, int]:
        total = max(0, int(total))
        round_no = 1
        while True:
            req = get_ex_round_requirement(pgid, round_no)
            if req <= 0 or total < req:
                return round_no, total, req
            total -= req
            round_no += 1

    def calc_ex_exp_limit_30_rounds(pgid: int) -> int:
        return sum(get_ex_round_requirement(pgid, i) for i in range(1, 31))

    char_missions = [m for m in master_missions if m.get("characterId") == cid]
    char_missions.sort(key=lambda x: x["id"])
    assert_and_reply(char_missions, f"找不到角色ID={cid}的任务数据")

    chara = await ctx.md.game_characters.find_by_id(cid)
    chara_name = (
        f"{chara.get('firstName', '')}{chara.get('givenName', '')}"
        if chara else (get_character_first_nickname(cid) or str(cid))
    )

    user_v2s = [item for item in (profile.get("userCharacterMissionV2s", []) or []) if item.get("characterId") == cid]
    user_statuses = [item for item in (profile.get("userCharacterMissionV2Statuses", []) or []) if item.get("characterId") == cid]
    user_items = [*user_v2s, *user_statuses]

    char_levels = await ctx.md.levels.find_by("levelType", "character", mode="all")
    char_levels = sorted(char_levels, key=lambda x: x["level"])
    char_level_total_exp = {int(x["level"]): int(x["totalExp"]) for x in char_levels}

    user_char = find_by(profile.get("userCharacters", []), "characterId", cid)
    assert_and_reply(user_char, "你的Suite数据来源没有提供userCharacters数据")
    cur_lv = int(user_char.get("characterRank", 1))
    cur_total_exp = int(user_char.get("totalExp", 0))
    if user_char.get("exp") is not None:
        cur_exp = int(user_char.get("exp", 0))
    else:
        cur_exp = max(0, cur_total_exp - char_level_total_exp.get(cur_lv, 0))

    pending_exp = 0
    for s in user_statuses:
        if s.get("missionStatus") != "achieved":
            continue
        pgid = int(s.get("parameterGroupId", 0))
        seq = int(s.get("seq", 0))
        pending_exp += pg_seq_exp.get((pgid, seq), 0)

    final_total_exp = cur_total_exp + pending_exp
    final_lv = 1
    final_lv_total = 0
    for lv_item in char_levels:
        if lv_item["totalExp"] <= final_total_exp:
            final_lv = int(lv_item["level"])
            final_lv_total = int(lv_item["totalExp"])
        else:
            break
    final_exp = final_total_exp - final_lv_total

    user_by_mission: dict[int, dict[str, int]] = {}
    user_by_type_progress: dict[str, int] = {}
    for item in user_items:
        mission_id = item.get("missionId")
        if mission_id is not None:
            cur = user_by_mission.setdefault(int(mission_id), {"progress": 0, "seq": 0})
            if item.get("progress") is not None:
                cur["progress"] = max(cur["progress"], int(item["progress"]))
            if item.get("seq") is not None:
                cur["seq"] = max(cur["seq"], int(item["seq"]))

        mission_type = item.get("characterMissionType")
        progress = item.get("progress")
        if mission_type and progress is not None:
            user_by_type_progress[mission_type] = max(user_by_type_progress.get(mission_type, 0), int(progress))

    ex_received_max_seq: dict[int, int] = {}
    for item in user_statuses:
        mission_id = item.get("missionId")
        seq = item.get("seq")
        if mission_id is None or seq is None:
            continue
        ex_received_max_seq[int(mission_id)] = max(ex_received_max_seq.get(int(mission_id), 0), int(seq))

    def get_ex_cleared_total(pgid: int, max_seq: int) -> int:
        if max_seq <= 0:
            return 0
        return sum(get_ex_round_requirement(pgid, i) for i in range(1, max_seq + 1))

    mission_rows = []
    for mission in char_missions:
        mission_id = int(mission["id"])
        mission_type = mission["characterMissionType"]
        pgid = int(mission["parameterGroupId"])
        is_ex = mission_type in CHAR_MISSION_EX_TYPES

        current = 0
        if is_ex:
            progress_raw = user_by_type_progress.get(mission_type, 0)
            received_seq = ex_received_max_seq.get(mission_id, 0)
            cleared_total = get_ex_cleared_total(pgid, received_seq)
            if progress_raw > 0:
                if progress_raw < cleared_total:
                    current = cleared_total + progress_raw
                else:
                    current = progress_raw
            else:
                current = cleared_total
        else:
            if mission_type in user_by_type_progress:
                current = user_by_type_progress[mission_type]
            elif mission_id in user_by_mission and user_by_mission[mission_id]["progress"] > 0:
                current = user_by_mission[mission_id]["progress"]
            elif mission_id in user_by_mission and user_by_mission[mission_id]["seq"] > 0:
                current = _get_pg_requirement_by_seq(pg_seq_requirements, pgid, user_by_mission[mission_id]["seq"])

        finite_upper = pg_max_requirement.get(pgid, 0)
        upper = None if is_ex else finite_upper
        ratio_upper = finite_upper if finite_upper > 0 else max(current, 1)
        ratio = 0.0 if ratio_upper <= 0 else min(current / ratio_upper, 1.0)
        next_need = None
        next_exp = None
        if is_ex:
            round_no, in_round_progress, round_need = calc_ex_round_and_progress(current, pgid)
            if round_need > 0:
                next_need = current + max(round_need - in_round_progress, 0)
                next_exp = get_ex_round_exp(pgid, round_no)
        else:
            for _, req, seq_exp in pg_seq_req_exp.get(pgid, []):
                if req > current:
                    next_need = req
                    next_exp = seq_exp
                    break

        mission_rows.append({
            "mission_id": mission_id,
            "mission_type": mission_type,
            "title": _char_mission_short_name(mission_type),
            "is_achievement": bool(mission.get("isAchievementMission", False)),
            "is_ex": is_ex,
            "current": current,
            "upper": upper,
            "ratio": ratio,
            "next_need": next_need,
            "next_exp": next_exp,
        })

    by_type = {item["mission_type"]: item for item in mission_rows}

    basic_rows = [item for item in mission_rows if not item["is_achievement"]]
    basic_order = [
        "collect_member",
        "collect_stamp",
        "collect_costume_3d",
        "collect_character_archive_voice",
        "collect_another_vocal",
        "read_mysekai_fixture_unique_character_talk",
        "read_area_talk",
    ]
    basic_order_idx = {name: i for i, name in enumerate(basic_order)}
    basic_rows.sort(key=lambda x: (basic_order_idx.get(x["mission_type"], 10**9), x["mission_id"]))
    ach_rows = [
        item for item in mission_rows
        if item["is_achievement"]
        and item["mission_type"] not in CHAR_MISSION_EX_TYPES
        and item["mission_type"] not in CHAR_MISSION_EX_BASE_TYPES
    ]

    header_style = TextStyle(font=DEFAULT_BOLD_FONT, size=24, color=(25, 25, 25, 255))
    sub_header_style = TextStyle(font=DEFAULT_BOLD_FONT, size=20, color=(35, 35, 35, 255))
    card_w = 520
    card_sep = 16

    with Canvas(bg=SEKAI_BLUE_BG).set_padding(BG_PADDING) as canvas:
        with VSplit().set_content_align('lt').set_item_align('lt').set_sep(16):
            await get_detailed_profile_card(ctx, profile, err_msg)

            with VSplit().set_content_align('l').set_item_align('l').set_sep(8).set_item_bg(roundrect_bg()):
                TextBox(
                    "各任务上限为MasterData中所规定的上限，并不一定是当前已实装资源总数",
                    TextStyle(font=DEFAULT_BOLD_FONT, size=18, color=(0, 0, 0)),
                    use_real_line_count=True,
                ).set_padding(12)

            with VSplit().set_bg(roundrect_bg()).set_padding(16).set_sep(12).set_content_align('lt').set_item_align('lt'):
                with HSplit().set_content_align('c').set_item_align('c').set_sep(12):
                    ImageBox(get_chara_icon_by_chara_id(cid), size=(48, 48))
                    TextBox(
                        f"{chara_name} 当前Lv.{cur_lv} EXP×{cur_exp} + 未领取EXP×{pending_exp} = 总计Lv.{final_lv} EXP×{final_exp}",
                        header_style,
                        use_real_line_count=True,
                    )

            with VSplit().set_bg(roundrect_bg()).set_padding(16).set_sep(12).set_content_align('lt').set_item_align('lt'):
                TextBox("基本任务", sub_header_style)
                for i in range(0, len(basic_rows), 2):
                    left = basic_rows[i]
                    right = basic_rows[i + 1] if i + 1 < len(basic_rows) else None
                    with HSplit().set_content_align('lt').set_item_align('lt').set_sep(card_sep):
                        _build_single_mission_card(
                            left["title"], left["current"], left["upper"], left["ratio"], card_w,
                            next_need=left.get("next_need"), next_exp=left.get("next_exp"),
                        )
                        if right:
                            _build_single_mission_card(
                                right["title"], right["current"], right["upper"], right["ratio"], card_w,
                                next_need=right.get("next_need"), next_exp=right.get("next_exp"),
                            )
                        else:
                            Spacer(w=card_w, h=1)

            with VSplit().set_bg(roundrect_bg()).set_padding(16).set_sep(12).set_content_align('lt').set_item_align('lt'):
                TextBox("成就", sub_header_style)

                play_live = by_type.get("play_live", {"current": 0, "upper": 0, "ratio": 0})
                play_live_ex = by_type.get("play_live_ex", {"current": 0, "upper": None, "ratio": 0})
                waiting_room = by_type.get("waiting_room", {"current": 0, "upper": 0, "ratio": 0})
                waiting_room_ex = by_type.get("waiting_room_ex", {"current": 0, "upper": None, "ratio": 0})

                play_live_ex_total = play_live_ex["current"]
                waiting_room_ex_total = waiting_room_ex["current"]
                play_live_ex_limit = calc_ex_exp_limit_30_rounds(101)
                waiting_room_ex_limit = calc_ex_exp_limit_30_rounds(102)
                play_live_ex_ratio = min(play_live_ex_total / max(play_live_ex_limit, 1), 1.0)
                waiting_room_ex_ratio = min(waiting_room_ex_total / max(waiting_room_ex_limit, 1), 1.0)

                play_live_round, _, _ = calc_ex_round_and_progress(play_live_ex_total, 101)
                waiting_room_round, _, _ = calc_ex_round_and_progress(waiting_room_ex_total, 102)

                with HSplit().set_content_align('lt').set_item_align('lt').set_sep(card_sep):
                    _build_dual_mission_card(
                        "队长次数",
                        play_live["current"], play_live["upper"], play_live["ratio"],
                        play_live.get("next_need"), play_live.get("next_exp"),
                        play_live_ex_total, play_live_ex_limit, play_live_ex_ratio,
                        play_live_ex.get("next_need"), play_live_ex.get("next_exp"),
                        card_w,
                        f"EX {play_live_round} 回目",
                    )
                    _build_dual_mission_card(
                        "休息室次数",
                        waiting_room["current"], waiting_room["upper"], waiting_room["ratio"],
                        waiting_room.get("next_need"), waiting_room.get("next_exp"),
                        waiting_room_ex_total, waiting_room_ex_limit, waiting_room_ex_ratio,
                        waiting_room_ex.get("next_need"), waiting_room_ex.get("next_exp"),
                        card_w,
                        f"EX {waiting_room_round} 回目",
                    )

                for i in range(0, len(ach_rows), 2):
                    left = ach_rows[i]
                    right = ach_rows[i + 1] if i + 1 < len(ach_rows) else None
                    with HSplit().set_content_align('lt').set_item_align('lt').set_sep(card_sep):
                        _build_single_mission_card(
                            left["title"], left["current"], left["upper"], left["ratio"], card_w,
                            next_need=left.get("next_need"), next_exp=left.get("next_exp"),
                        )
                        if right:
                            _build_single_mission_card(
                                right["title"], right["current"], right["upper"], right["ratio"], card_w,
                                next_need=right.get("next_need"), next_exp=right.get("next_exp"),
                            )
                        else:
                            Spacer(w=card_w, h=1)

    add_watermark(canvas)
    return await canvas.get_img()


# ======================= 指令处理 ======================= #

# 绑定id或查询绑定id
pjsk_bind = SekaiCmdHandler([
    "/pjsk bind", "/pjsk id",
    "/绑定", "/pjsk 绑定"
], parse_uid_arg=False)
pjsk_bind.check_cdrate(cd).check_wblist(gbl)
@pjsk_bind.handle()
async def _(ctx: SekaiHandlerContext):
    args = ctx.get_args().strip()
    args = ''.join([c for c in args if c.isdigit()])
    
    # -------------- 查询 -------------- #

    if not args:
        has_any = False
        msg = ""
        for region in ALL_SERVER_REGIONS:
            region_ctx = SekaiHandlerContext.from_region(region)
            main_uid = get_player_bind_id(region_ctx, ctx.user_id, check_bind=False)

            lines = []
            for i in range(get_player_bind_count(region_ctx, ctx.user_id)):
                uid = get_player_bind_id(region_ctx, ctx.user_id, index=i)
                is_main = (uid == main_uid)
                uid = process_hide_uid(ctx, uid, keep=6)
                line = f"[{i+1}] {uid}"
                if is_main:
                    line = "*" + line
                lines.append(line)

            if lines:
                has_any = True
                msg += f"【{get_region_name(region)}】\n" + '\n'.join(lines) + '\n'

        if not has_any:
            return await ctx.asend_reply_msg("你还没有绑定过游戏ID，请使用\"/绑定 游戏ID\"进行绑定")
        
        msg += """
标注星号的是查询时默认的主账号，其他账号需要手动指定，例如"/个人信息 u2"查询第二个账号的个人信息
""".strip()
        return await ctx.asend_fold_msg_adaptive(msg.strip())

    # -------------- 绑定 -------------- #

    # 检查是否在黑名单中
    assert_and_reply(not check_uid_in_blacklist(args), f"该游戏ID({args})已被拉入黑名单，无法绑定")
    
    # 检查有效的服务器
    checked_regions = []
    async def check_bind(region: str) -> Optional[Tuple[str, str, str]]:
        try:
            region_ctx = SekaiHandlerContext.from_region(region)
            if not get_gameapi_config(region_ctx).profile_api_url:
                return None
            # 检查格式
            if not validate_uid(region_ctx, args):
                return region, None, f"ID格式错误"
            checked_regions.append(get_region_name(region))
            profile = await get_basic_profile(region_ctx, args, use_cache=False, use_remote_cache=False, raise_when_no_found=False)
            if not profile:
                return region, None, "找不到该ID的玩家"
            user_name = profile['user']['name']
            return region, user_name, None
        except Exception as e:
            logger.warning(f"在 {region} 服务器尝试绑定失败: {get_exc_desc(e)}")
            return region, None, "内部错误，请稍后重试"
        
    check_results = await asyncio.gather(*[check_bind(region) for region in ALL_SERVER_REGIONS])
    check_results = [res for res in check_results if res]
    ok_check_results = [res for res in check_results if res[2] is None]

    if not ok_check_results:
        reply_text = f"所有支持的服务器尝试绑定失败，请检查ID是否正确"
        for region, _, err_msg in check_results:
            if err_msg:
                reply_text += f"\n{get_region_name(region)}: {err_msg}"
        return await ctx.asend_reply_msg(reply_text)
    
    if len(ok_check_results) > 1:
        await ctx.asend_reply_msg(f"该ID在多个服务器都存在！默认绑定找到的第一个服务器")
    region, user_name, _ = ok_check_results[0]
    qid = str(ctx.user_id)
    uid = args

    region_ctx = SekaiHandlerContext.from_region(region)
    last_bind_main_id = get_player_bind_id(region_ctx, ctx.user_id, check_bind=False)

    # 检查绑定次数限制
    if not check_superuser(ctx.event):
        date = get_date_str()
        all_daily_info = bind_history_db.get(f"{region}_daily", {})
        daily_info = all_daily_info.get(qid, { 'date': date, 'ids': [] })
        if daily_info['date'] != date:
            daily_info = { 'date': date, 'ids': [] }

        today_ids = set(daily_info.get('ids', []))
        today_ids.add(uid)
        if last_bind_main_id:
            today_ids.add(last_bind_main_id) # 当前绑定的id也算在内

        daily_info['ids'] = list(today_ids)
        if len(daily_info['ids']) > DAILY_BIND_LIMITS.get().get(region, 1e9):
            return await ctx.asend_reply_msg(f"你今日绑定{get_region_name(region)}帐号的数量已达上限")
        all_daily_info[qid] = daily_info
        bind_history_db.set(f"{region}_daily", all_daily_info)

    msg = f"{get_region_name(region)}绑定成功: {user_name}"

    # 如果以前没有绑定过其他区服，设置默认服务器
    other_bind = None
    for r in ALL_SERVER_REGIONS:
        if r == region: continue
        other_bind = other_bind or get_player_bind_id(SekaiHandlerContext.from_region(r), ctx.user_id, check_bind=False)
    default_region = get_user_default_region(ctx.user_id, None)
    if not other_bind and not default_region:
        msg += f"\n已设置你的默认服务器为{get_region_name(region)}，如需修改可使用\"/pjsk服务器\""
        set_user_default_region(ctx.user_id, region)
    if default_region and default_region != region:
        msg += f"\n你的默认服务器为{get_region_name(default_region)}，查询{get_region_name(region)}需加前缀{region}，或使用\"/pjsk服务器\"修改默认服务器"

    # 如果该区服以前没有绑定过，设置默认隐藏id
    assert_and_reply(not check_qid_in_blacklist(ctx.user_id), f"该QQ号({ctx.user_id})已被拉入黑名单，无法绑定")

    if not last_bind_main_id:
        if is_cloud_account_enabled():
            try:
                cloud_account_api.set_preferences(region, ctx.user_id, hide_id=True, operator=str(ctx.user_id))
            except Exception as e:
                raise ReplyException(f"云端写入默认隐藏ID设置失败: {e}")
        else:
            lst = profile_db.get("hide_id_list", {})
            if region not in lst:
                lst[region] = []
            if ctx.user_id not in lst[region]:
                lst[region].append(ctx.user_id)
            profile_db.set("hide_id_list", lst)

    # 进行绑定
    bind_msg = add_player_bind_id(region_ctx, ctx.user_id, uid, set_main=True)
    msg += "\n" + bind_msg

    # 保存绑定历史
    bind_history = bind_history_db.get("history", {})
    if qid not in bind_history:
        bind_history[qid] = []
    bind_history[qid].append({
        "time": int(time.time() * 1000),
        "region": region,
        "uid": uid,
    })
    bind_history_db.set("history", bind_history)
    
    return await ctx.asend_reply_msg(msg.strip())


# 解绑id
pjsk_unbind = SekaiCmdHandler([
    "/pjsk unbind", "/pjsk解绑", "/解绑",
], parse_uid_arg=False)
pjsk_unbind.check_cdrate(cd).check_wblist(gbl)
@pjsk_unbind.handle()
async def _(ctx: SekaiHandlerContext):
    args = ctx.get_args().strip().lower()
    qid = ctx.user_id
    try:
        args = args.replace('u', '')
        index = int(args) - 1
    except:
        raise ReplyException(f"""
解除第x个账号绑定:"{ctx.original_trigger_cmd} x"
发送"/绑定"查询已绑定的账号
""".strip())
    
    msg = remove_player_bind_id(ctx, qid, index=index)
    return await ctx.asend_reply_msg(msg)


# 设置主账号
pjsk_set_main = SekaiCmdHandler([
    "/pjsk set main", "/pjsk主账号", "/设置主账号", "/主账号",
], parse_uid_arg=False)
pjsk_set_main.check_cdrate(cd).check_wblist(gbl)
@pjsk_set_main.handle()
async def _(ctx: SekaiHandlerContext):
    args = ctx.get_args().strip()
    qid = ctx.user_id
    try:
        args = args.replace('u', '')
        index = int(args) - 1
    except:
        raise ReplyException(f"""
使用方式: 
设置主账号为你第x个绑定的账号: {ctx.original_trigger_cmd} x
""".strip())
    
    msg = set_player_main_bind_id(ctx, qid, index=index)
    return await ctx.asend_reply_msg(msg)


# 交换绑定账号顺序
pjsk_swap_bind = SekaiCmdHandler([
    "/pjsk swap bind", "/pjsk交换绑定", 
    "/交换绑定", "/绑定交换", "/交换账号", "/交换账号顺序",
], parse_uid_arg=False)
pjsk_swap_bind.check_cdrate(cd).check_wblist(gbl)
@pjsk_swap_bind.handle()
async def _(ctx: SekaiHandlerContext):
    args = ctx.get_args().strip().split()
    qid = ctx.user_id
    try:
        index1 = int(args[0].replace('u', '')) - 1
        index2 = int(args[1].replace('u', '')) - 1
    except:
        raise ReplyException(f"""
使用方式:
交换你绑定的第x个和第y个账号的位置: {ctx.original_trigger_cmd} x y
""".strip())
    
    msg = swap_player_bind_id(ctx, qid, index1=index1, index2=index2)
    return await ctx.asend_reply_msg(msg)


# 隐藏抓包信息
pjsk_hide_suite = SekaiCmdHandler([
    "/pjsk hide suite",
    "/pjsk隐藏抓包", "/隐藏抓包",
])
pjsk_hide_suite.check_cdrate(cd).check_wblist(gbl)
@pjsk_hide_suite.handle()
async def _(ctx: SekaiHandlerContext):
    if is_cloud_account_enabled():
        try:
            cloud_account_api.set_preferences(ctx.region, ctx.user_id, hide_suite=True, operator=str(ctx.user_id))
        except Exception as e:
            raise ReplyException(f"云端设置隐藏抓包失败: {e}")
    else:
        lst = profile_db.get("hide_suite_list", {})
        if ctx.region not in lst:
            lst[ctx.region] = []
        if ctx.user_id not in lst[ctx.region]:
            lst[ctx.region].append(ctx.user_id)
        profile_db.set("hide_suite_list", lst)
    return await ctx.asend_reply_msg(f"已隐藏{get_region_name(ctx.region)}抓包信息")
    

# 展示抓包信息
pjsk_show_suite = SekaiCmdHandler([
    "/pjsk show suite",
    "/pjsk显示抓包", "/pjsk展示抓包", "/展示抓包",
])
pjsk_show_suite.check_cdrate(cd).check_wblist(gbl)
@pjsk_show_suite.handle()
async def _(ctx: SekaiHandlerContext):
    if is_cloud_account_enabled():
        try:
            cloud_account_api.set_preferences(ctx.region, ctx.user_id, hide_suite=False, operator=str(ctx.user_id))
        except Exception as e:
            raise ReplyException(f"云端取消隐藏抓包失败: {e}")
    else:
        lst = profile_db.get("hide_suite_list", {})
        if ctx.region not in lst:
            lst[ctx.region] = []
        if ctx.user_id in lst[ctx.region]:
            lst[ctx.region].remove(ctx.user_id)
        profile_db.set("hide_suite_list", lst)
    return await ctx.asend_reply_msg(f"已展示{get_region_name(ctx.region)}抓包信息")


# 隐藏id信息
pjsk_hide_id = SekaiCmdHandler([
    "/pjsk hide id",
    "/pjsk隐藏id", "/pjsk隐藏ID", "/隐藏id", "/隐藏ID",
])
pjsk_hide_id.check_cdrate(cd).check_wblist(gbl)
@pjsk_hide_id.handle()
async def _(ctx: SekaiHandlerContext):
    if is_cloud_account_enabled():
        try:
            cloud_account_api.set_preferences(ctx.region, ctx.user_id, hide_id=True, operator=str(ctx.user_id))
        except Exception as e:
            raise ReplyException(f"云端设置隐藏ID失败: {e}")
    else:
        lst = profile_db.get("hide_id_list", {})
        if ctx.region not in lst:
            lst[ctx.region] = []
        if ctx.user_id not in lst[ctx.region]:
            lst[ctx.region].append(ctx.user_id)
        profile_db.set("hide_id_list", lst)
    return await ctx.asend_reply_msg(f"已隐藏{get_region_name(ctx.region)}ID信息")


# 展示id信息
pjsk_show_id = SekaiCmdHandler([
    "/pjsk show id",
    "/pjsk显示id", "/pjsk显示ID", "/pjsk展示id", "/pjsk展示ID",
    "/展示id", "/展示ID", "/显示id", "/显示ID",
])
pjsk_show_id.check_cdrate(cd).check_wblist(gbl)
@pjsk_show_id.handle()
async def _(ctx: SekaiHandlerContext):
    if is_cloud_account_enabled():
        try:
            cloud_account_api.set_preferences(ctx.region, ctx.user_id, hide_id=False, operator=str(ctx.user_id))
        except Exception as e:
            raise ReplyException(f"云端取消隐藏ID失败: {e}")
    else:
        lst = profile_db.get("hide_id_list", {})
        if ctx.region not in lst:
            lst[ctx.region] = []
        if ctx.user_id in lst[ctx.region]:
            lst[ctx.region].remove(ctx.user_id)
        profile_db.set("hide_id_list", lst)
    return await ctx.asend_reply_msg(f"已展示{get_region_name(ctx.region)}ID信息")


# 查询单角色角色等级任务总览
pjsk_character_rank_mission = SekaiCmdHandler([
    "/cr任务", "/角色等级任务",
])
pjsk_character_rank_mission.check_cdrate(cd).check_wblist(gbl)
@pjsk_character_rank_mission.handle()
async def _(ctx: SekaiHandlerContext):
    help_msg = f"""
使用方式:
1. {ctx.original_trigger_cmd} 角色名
2. {ctx.original_trigger_cmd} 角色名 all 任务名
示例:
{ctx.original_trigger_cmd} miku
{ctx.original_trigger_cmd} miku all 队长次数
发送“/cr任务 help”获取详细帮助
""".strip()
    raw_args = ctx.get_args().strip()
    assert_and_reply(raw_args, help_msg)
    if raw_args.lower() in ("help", "帮助"):
        help_text = f"""
# CR任务

查询指定角色的角色等级任务进度，或查看某个任务的全量档位表。  
需要📡抓包数据。  
支持服务器: `所有`

## 基础用法

- `{ctx.original_trigger_cmd} miku`
- `{ctx.original_trigger_cmd} miku all 队长次数`

## 查询模式

- `角色名`
  查询该角色的角色任务总览
- `角色名 all 任务名`
  查询该任务的全量档位、累计需求和累计EXP

## 说明

- `队长次数` 和 `休息室次数` 在 `all` 视图下会同时显示普通任务和EX任务
- 其他任务在 `all` 视图下只显示对应单个任务表

## 可用任务名示例

- `队长次数` `队长`
- `休息室次数` `休息室` `控制室`
- `服装` `衣装`
- `表情` `贴纸`
- `区域对话`
- `前篇` `前编`
- `后篇` `后编`
- `anvo`
- `单人家具` `单人道具`
- `团家具`
- `树花` `属性家具` `属性道具` `植物`
- `卡面` `图鉴` `成员`
- `4星技能` `四星技能` `四星slv` `4星slv`
- `低星技能` `低星slv`
- `4星专精` `四星专精` `四星突破` `4星突破` `4星mr` `四星mr`
- `低星专精` `低星突破` `低星mr`
- `台词` `语音`
- `ms家具` `烤森家具`
- `ms画布` `烤森画布`
- `ms对话` `烤森对话`
""".strip()
        return await ctx.asend_reply_msg(await get_image_cq(
            await markdown_to_image(help_text, width=760),
            low_quality=True,
        ))
    nickname, rest = extract_nickname_from_args(raw_args)
    assert_and_reply(nickname, f"未识别到角色名称\n{help_msg}")
    cid = get_cid_by_nickname(nickname)
    assert_and_reply(cid is not None, f"角色名无效: {nickname}")

    rest = rest.strip()
    if rest:
        from .education import (
            extract_character_rank_all_flag,
            extract_character_rank_mission_type,
            compose_character_rank_mission_all_image,
        )
        show_all, rest = extract_character_rank_all_flag(rest)
        if show_all:
            mission_type, rest = extract_character_rank_mission_type(rest)
            assert_and_reply(mission_type is not None and not rest.strip(), f"未识别到角色等级任务名\n{help_msg}")
            return await ctx.asend_reply_msg(await get_image_cq(
                await compose_character_rank_mission_all_image(ctx, ctx.user_id, cid, mission_type),
                low_quality=True,
            ))
        assert_and_reply(False, f"参数无法解析: {rest}\n{help_msg}")

    profile, err_msg = await get_detailed_profile(
        ctx,
        ctx.user_id,
        filter=get_detailed_profile_card_filter("userCharacterMissionV2s", "userCharacterMissionV2Statuses", "userCharacters"),
        raise_exc=True,
    )

    return await ctx.asend_reply_msg(await get_image_cq(
        await compose_character_rank_mission_overview_image(ctx, profile, err_msg, cid),
        low_quality=True,
    ))


# 查询个人名片
pjsk_info = SekaiCmdHandler([
    "/pjsk profile",
    "/个人信息", "/名片", "/pjsk 个人信息", "/pjsk 名片",
])
pjsk_info.check_cdrate(cd).check_wblist(gbl)
@pjsk_info.handle()
async def _(ctx: SekaiHandlerContext):
    args = ctx.get_args().strip()
    vertical = None

    for keyword in PROFILE_HORIZONTAL_KEYWORDS:
        if keyword in args:
            vertical = False
            args = args.replace(keyword, '', 1).strip()
            break
    for keyword in PROFILE_VERTICAL_KEYWORDS:
        if keyword in args:
            vertical = True
            args = args.replace(keyword, '', 1).strip()
            break

    uid = get_player_bind_id(ctx)
    profile = await get_basic_profile(ctx, uid, use_cache=True, use_remote_cache=False)
    logger.info(f"绘制名片 region={ctx.region} uid={uid}")
    return await ctx.asend_reply_msg(await get_image_cq(
        await compose_profile_image(ctx, profile, vertical=vertical),
        low_quality=True, quality=95,
    ))


# 查询注册时间
pjsk_reg_time = SekaiCmdHandler([
    "/pjsk reg time",
    "/注册时间", "/pjsk 注册时间", "/查时间",
])
pjsk_reg_time.check_cdrate(cd).check_wblist(gbl)
@pjsk_reg_time.handle()
async def _(ctx: SekaiHandlerContext):
    uid = get_player_bind_id(ctx)
    reg_time = get_register_time(ctx.region, uid)
    elapsed = datetime.now() - reg_time
    region_name = get_region_name(ctx.region)
    return await ctx.asend_reply_msg(f"{region_name}注册时间: {reg_time.strftime('%Y-%m-%d %H:%M:%S')} ({elapsed.days}天前)")


# 检查profile服务器状态
pjsk_check_service = SekaiCmdHandler([
    "/pjsk check service", "/pcs", "/pjsk检查服务状态",
])
pjsk_check_service.check_cdrate(cd).check_wblist(gbl)
@pjsk_check_service.handle()
async def _(ctx: SekaiHandlerContext):
    url = get_gameapi_config(ctx).api_status_url
    assert_and_reply(url, f"暂无 {ctx.region} 的查询服务器")
    try:
        data = await request_gameapi(url)
        assert data['status'] == 'ok'
    except Exception as e:
        logger.print_exc(f"profile查询服务状态异常")
        return await ctx.asend_reply_msg(f"profile查询服务异常: {str(e)}")
    return await ctx.asend_reply_msg("profile查询服务正常")


# ================================ Suite 用户命令 ================================ #
# 这里统一收拢 suite 新链路相关的用户入口：
# - `/抓包模式`：控制 suite_source_mode
# - `/授权`：发起 Haruki OAuth2 授权
# - `/抓包信息`：按“当前实际会走的单一链路”展示状态


# 设置 suite 抓包来源模式
pjsk_data_mode = SekaiCmdHandler([
    "/pjsk data mode",
    "/pjsk抓包模式", "/pjsk抓包获取模式", "/抓包模式",
])
pjsk_data_mode.check_cdrate(cd).check_wblist(gbl)
@pjsk_data_mode.handle()
async def _(ctx: SekaiHandlerContext):
    suite_source_modes = profile_db.get("suite_source_modes", {})
    cur_mode = get_user_suite_source_mode(ctx, ctx.user_id)
    help_text = f"""
你的{get_region_name(ctx.region)}Suite数据获取模式: {cur_mode}
---
使用\"{ctx.original_trigger_cmd} 模式名\"来切换模式，可用模式名如下:
【default】
自己查自己时先走Haruki工具箱，失败再回退Haruki Public-API（推荐）
【oauth】
自己查自己时只走Haruki工具箱，不再回退Public-API
【public】
统一只走Haruki Public-API
""".strip()

    ats = ctx.get_at_qids()
    if ats and ats[0] != int(ctx.bot.self_id):
        qid = ats[0]
        assert_and_reply(check_superuser(ctx.event), "只有超级管理能修改别人的模式")
    else:
        qid = ctx.user_id

    raw_args = ctx.get_args().strip().lower()
    if not raw_args:
        return await ctx.asend_reply_msg(help_text)
    assert_and_reply(raw_args in VALID_SUITE_SOURCE_MODES or raw_args in VALID_DATA_MODES, help_text)

    normalized_mode = _normalize_suite_source_mode(raw_args)
    if is_cloud_account_enabled():
        try:
            cloud_account_api.set_preferences(
                ctx.region,
                qid,
                suite_source_mode=normalized_mode,
                operator=str(ctx.user_id),
            )
        except Exception as e:
            raise ReplyException(f"云端切换Suite抓包模式失败: {e}")
    else:
        if ctx.region not in suite_source_modes:
            suite_source_modes[ctx.region] = {}
        suite_source_modes[ctx.region][str(qid)] = normalized_mode
        profile_db.set("suite_source_modes", suite_source_modes)

    compat_note = ""
    if raw_args != normalized_mode:
        compat_note = f"\n（输入的旧模式 {raw_args} 已自动归一化为 {normalized_mode}）"

    if qid == ctx.user_id:
        return await ctx.asend_reply_msg(
            f"切换{get_region_name(ctx.region)}Suite抓包模式:\n{cur_mode} -> {normalized_mode}{compat_note}"
        )
    return await ctx.asend_reply_msg(
        f"切换 {qid} 的{get_region_name(ctx.region)}Suite抓包模式:\n{cur_mode} -> {normalized_mode}{compat_note}"
    )


async def reply_oauth_authorize_link(ctx: SekaiHandlerContext, url: str):
    """
    优先私聊授权链接。

    当前阶段不做中转页，直接投递 Haruki 授权链接：
    - 能私聊：群里只提示“已私发”
    - 不能私聊：群里先明确建议走临时会话，再带风险提示后回落群内发链接
    """
    private_msg = f"请在浏览器中打开以下链接完成 Haruki OAuth2 授权（链接 10 分钟内有效）：\n{url}"

    if not ctx.group_id:
        return await ctx.asend_reply_msg(private_msg)

    # 这里故意不走 `send_private_msg_by_bot()` 的好友预检查。
    # 原因是 OAuth 授权链接是极少数“值得先试发一次私聊”的场景：
    # - 某些实现下，即使不是好友，也可能允许群临时会话私聊
    # - 如果我们先查好友列表，就会把这类可发送场景提前误判成失败
    #
    # 这里的策略是：
    # 1. 先 best-effort 试发私聊
    # 2. 成功则群里只提示“已私发”
    # 3. 失败再按既定策略回落群内，并明确建议用户改走临时会话
    async def try_send_private_once() -> bool:
        candidate_bots = []
        if getattr(ctx, "bot", None):
            candidate_bots.append(ctx.bot)
        try:
            private_bot = await aget_private_bot(int(ctx.user_id))
        except Exception as e:
            logger.warning(f"[sekai] 获取可用私聊 Bot 失败 qid={ctx.user_id}: {e}")
            private_bot = None
        if private_bot and all(str(bot.self_id) != str(private_bot.self_id) for bot in candidate_bots):
            candidate_bots.append(private_bot)

        for bot in candidate_bots:
            try:
                await bot.send_private_msg(user_id=int(ctx.user_id), message=private_msg)
                return True
            except Exception as e:
                logger.warning(
                    f"[sekai] OAuth 授权链接私聊发送失败 "
                    f"qid={ctx.user_id} bot={getattr(bot, 'self_id', '?')}: {e}"
                )
        return False

    sent = await try_send_private_once()
    if sent:
        return await ctx.asend_reply_msg(
            "授权链接已私发，请注意查收。若未收到，建议点击 Bot 头像进入临时会话后重新发送 /授权。"
        )

    return await ctx.asend_reply_msg(
        "建议优先点击 Bot 头像进入临时会话后发送 /授权 获取链接。\n"
        "当前未能通过私聊发送授权链接，下面将直接在群内发送；若继续点击，即视为你已知晓公开发送带来的风险：\n"
        f"{url}"
    )


sekai_oauth_authorize = SekaiCmdHandler([
    "/授权", "/sekai授权", "/pjsk授权",
], parse_uid_arg=False, parse_data_mode_arg=False)
sekai_oauth_authorize.check_cdrate(cd).check_wblist(gbl)
@sekai_oauth_authorize.handle()
async def _(ctx: SekaiHandlerContext):
    qid = str(ctx.user_id)
    try:
        # 授权前先清掉本进程中的旧状态，减少“刚授权成功但短时间还走旧缓存”的错觉。
        clear_oauth_runtime_cache(qid)
        cloud_account_api.invalidate_state_cache(qid=qid)
        url = await build_authorize_url(qid)
    except Exception as e:
        raise ReplyException(f"生成授权链接失败: {e}")

    return await reply_oauth_authorize_link(ctx, url)


# 查询 suite 抓包数据状态
pjsk_check_data = SekaiCmdHandler([
    "/pjsk check data",
    "/pjsk抓包", "/pjsk抓包状态", "/pjsk抓包数据", "/pjsk抓包查询", "/抓包数据", "/抓包状态", "/抓包信息",
])
pjsk_check_data.check_cdrate(cd).check_wblist(gbl)
@pjsk_check_data.handle()
async def _(ctx: SekaiHandlerContext):
    cqs = extract_cq_code(ctx.get_msg())
    qid = int(cqs['at'][0]['qq']) if 'at' in cqs else ctx.user_id
    uid = get_player_bind_id(ctx)
    suite_mode = get_user_suite_source_mode(ctx, qid)

    msg = f"{process_hide_uid(ctx, uid, keep=6)}({ctx.region.upper()}) Suite数据\n"
    profile, err = await get_detailed_profile(
        ctx,
        qid,
        raise_exc=False,
        mode=suite_mode,
        filter=["upload_time"],
        strict=False,
    )

    if err:
        source_label = get_suite_expected_source_name(ctx, qid, str(uid), suite_mode)
        err = err[err.find(']') + 1:].strip() if ']' in err else err
        msg += f"[{source_label}]\n获取失败: {err}\n"
    else:
        source_label = get_suite_source_display_name(profile.get("source"))
        msg += f"[{source_label}]\n"
        upload_time = datetime.fromtimestamp(profile["upload_time"] / 1000)
        upload_time_text = upload_time.strftime("%m-%d %H:%M:%S") + f"({get_readable_datetime(upload_time, show_original_time=False)})"
        if local_source := profile.get("local_source"):
            upload_time_text = local_source + " " + upload_time_text
        msg += f"{upload_time_text}\n"
        if warning_text := get_suite_public_warning_text(ctx, profile):
            msg += f"{warning_text}\n"

    if oauth_status_text := get_suite_oauth_status_text(ctx, qid, str(uid), suite_mode, profile, err):
        msg += f"{oauth_status_text}\n"

    msg += f"---\n"
    msg += f"该指令查询Suite数据，查询Mysekai数据请使用\"/{ctx.region}msd\"\n"
    msg += f"发送\"/抓包\"获取抓包教程"

    return await ctx.asend_reply_msg(msg.strip())






# 添加游戏id到黑名单
pjsk_blacklist = CmdHandler([
    "/pjsk blacklist add", "/pjsk add blacklist",
    "/pjsk黑名单添加", "/pjsk添加黑名单",
], logger)
pjsk_blacklist.check_cdrate(cd).check_wblist(gbl).check_superuser()
@pjsk_blacklist.handle()
async def _(ctx: HandlerContext):
    region, uid, reason = _parse_uid_blacklist_args(ctx.get_args().strip())
    if is_cloud_account_enabled():
        try:
            cloud_account_api.add_blacklist(
                "uid",
                uid,
                region=region,
                reason=reason or None,
                operator=str(ctx.user_id),
            )
        except Exception as e:
            raise ReplyException(f"添加游戏ID黑名单失败: {e}")
    else:
        blacklist = [str(item) for item in profile_db.get("blacklist", [])]
        if uid in blacklist:
            return await ctx.asend_reply_msg(f"ID {uid} 已在黑名单中")
        blacklist.append(uid)
        profile_db.set("blacklist", blacklist)
    extra = f"\n原因: {reason}" if reason else ""
    return await ctx.asend_reply_msg(f"已将 {region.upper()} 游戏ID {uid} 加入黑名单{extra}")
    assert_and_reply(args, "请提供要添加的游戏ID")
    blacklist = profile_db.get("blacklist", [])
    if args in blacklist:
        return await ctx.asend_reply_msg(f"ID {args} 已在黑名单中")
    blacklist.append(args)
    profile_db.set("blacklist", blacklist)
    return await ctx.asend_reply_msg(f"ID {args} 已添加到黑名单中")


# 移除游戏id到黑名单
pjsk_blacklist_remove = CmdHandler([
    "/pjsk blacklist remove", "/pjsk blacklist del", "/pjsk remove blacklist", "/pjsk del blacklist",
    "/pjsk黑名单移除", "/pjsk移除黑名单", "/pjsk删除黑名单",
], logger)
pjsk_blacklist_remove.check_cdrate(cd).check_wblist(gbl).check_superuser()
@pjsk_blacklist_remove.handle()
async def _(ctx: HandlerContext):
    region, uid, _ = _parse_uid_blacklist_args(ctx.get_args().strip())
    if is_cloud_account_enabled():
        try:
            removed = cloud_account_api.remove_blacklist(
                "uid",
                uid,
                region=region,
                operator=str(ctx.user_id),
            )
        except Exception as e:
            raise ReplyException(f"移除游戏ID黑名单失败: {e}")
        if not removed:
            return await ctx.asend_reply_msg(f"{region.upper()} 游戏ID {uid} 不在黑名单中")
    else:
        blacklist = [str(item) for item in profile_db.get("blacklist", [])]
        if uid not in blacklist:
            return await ctx.asend_reply_msg(f"ID {uid} 不在黑名单中")
        blacklist.remove(uid)
        profile_db.set("blacklist", blacklist)
    return await ctx.asend_reply_msg(f"已将 {region.upper()} 游戏ID {uid} 移出黑名单")
    assert_and_reply(args, "请提供要移除的游戏ID")
    blacklist = profile_db.get("blacklist", [])
    if args not in blacklist:
        return await ctx.asend_reply_msg(f"ID {args} 不在黑名单中")
    blacklist.remove(args)
    profile_db.set("blacklist", blacklist)
    return await ctx.asend_reply_msg(f"ID {args} 已从黑名单中移除")


# 验证用户游戏帐号
pjsk_qid_blacklist = CmdHandler([
    "/pjsk qid blacklist add", "/pjsk add qid blacklist",
    "/pjsk qq blacklist add", "/pjsk add qq blacklist",
], logger)
pjsk_qid_blacklist.check_cdrate(cd).check_wblist(gbl).check_superuser()
@pjsk_qid_blacklist.handle()
async def _(ctx: HandlerContext):
    qid, reason = _parse_qid_blacklist_args(ctx.get_args().strip())
    if is_cloud_account_enabled():
        try:
            cloud_account_api.add_blacklist(
                "qid",
                qid,
                reason=reason or None,
                operator=str(ctx.user_id),
            )
        except Exception as e:
            raise ReplyException(f"添加QQ黑名单失败: {e}")
    else:
        blacklist = _get_local_qid_blacklist()
        if qid in blacklist:
            return await ctx.asend_reply_msg(f"QQ {qid} 已在黑名单中")
        blacklist.append(qid)
        profile_db.set("qid_blacklist", blacklist)
    extra = f"\n原因: {reason}" if reason else ""
    return await ctx.asend_reply_msg(f"已将 QQ {qid} 加入黑名单{extra}")


pjsk_qid_blacklist_remove = CmdHandler([
    "/pjsk qid blacklist remove", "/pjsk qid blacklist del",
    "/pjsk remove qid blacklist", "/pjsk del qid blacklist",
    "/pjsk qq blacklist remove", "/pjsk qq blacklist del",
], logger)
pjsk_qid_blacklist_remove.check_cdrate(cd).check_wblist(gbl).check_superuser()
@pjsk_qid_blacklist_remove.handle()
async def _(ctx: HandlerContext):
    qid, _ = _parse_qid_blacklist_args(ctx.get_args().strip())
    if is_cloud_account_enabled():
        try:
            removed = cloud_account_api.remove_blacklist(
                "qid",
                qid,
                operator=str(ctx.user_id),
            )
        except Exception as e:
            raise ReplyException(f"移除QQ黑名单失败: {e}")
        if not removed:
            return await ctx.asend_reply_msg(f"QQ {qid} 不在黑名单中")
    else:
        blacklist = _get_local_qid_blacklist()
        if qid not in blacklist:
            return await ctx.asend_reply_msg(f"QQ {qid} 不在黑名单中")
        blacklist.remove(qid)
        profile_db.set("qid_blacklist", blacklist)
    return await ctx.asend_reply_msg(f"已将 QQ {qid} 移出黑名单")


pjsk_blacklist_list = CmdHandler([
    "/pjsk blacklist list", "/pjsk list blacklist",
    "/pjsk黑名单列表", "/pjsk查看黑名单",
], logger)
pjsk_blacklist_list.check_cdrate(cd).check_wblist(gbl).check_superuser()
@pjsk_blacklist_list.handle()
async def _(ctx: HandlerContext):
    return await ctx.asend_fold_msg_adaptive(_format_uid_blacklist_list_text())


pjsk_qid_blacklist_list = CmdHandler([
    "/pjsk qid blacklist list", "/pjsk list qid blacklist",
    "/pjsk qq blacklist list", "/pjsk list qq blacklist",
], logger)
pjsk_qid_blacklist_list.check_cdrate(cd).check_wblist(gbl).check_superuser()
@pjsk_qid_blacklist_list.handle()
async def _(ctx: HandlerContext):
    return await ctx.asend_fold_msg_adaptive(_format_qid_blacklist_list_text())


verify_game_account = SekaiCmdHandler([
    "/pjsk verify", "/pjsk验证",
])
verify_game_account.check_cdrate(cd).check_wblist(gbl).check_cdrate(verify_rate_limit)
@verify_game_account.handle()
async def _(ctx: SekaiHandlerContext):
    await ctx.block_region(key=str(ctx.user_id))
    await verify_user_game_account(ctx)


# 查询用户验证过的游戏ID列表
get_verified_uids = SekaiCmdHandler([
    "/pjsk verify list", "/pjsk验证列表", "/pjsk验证状态", 
])
get_verified_uids.check_cdrate(cd).check_wblist(gbl)
@get_verified_uids.handle()
async def _(ctx: SekaiHandlerContext):
    uids = get_user_verified_uids(ctx)
    msg = ""
    region_name = get_region_name(ctx.region)
    if not uids:
        msg += f"你还没有验证过任何{region_name}游戏ID\n"
    else:
        msg += f"你验证过的{region_name}游戏ID:\n"
        for uid in uids:
            msg += process_hide_uid(ctx, uid, keep=6) + "\n"
    msg += f"---\n"
    msg += f"使用\"/{ctx.region}pjsk验证\"进行验证"
    return await ctx.asend_reply_msg(msg)


# 上传个人信息背景图片
upload_profile_bg = SekaiCmdHandler([
    "/pjsk upload profile bg", "/pjsk upload profile background",
    "/上传个人信息背景", "/上传个人信息图片", "/上传个人背景", "/上传个人信息",
])
upload_profile_bg.check_cdrate(cd).check_wblist(gbl).check_cdrate(profile_bg_upload_rate_limit)
@upload_profile_bg.handle()
async def _(ctx: SekaiHandlerContext):
    await ctx.block_region(key=str(ctx.user_id))

    args = ctx.get_args().strip()
    force = False
    if 'force' in args and check_superuser(ctx.event):
        force = True
        args = args.replace('force', '').strip()

    uid = await get_uid_and_check_verified(ctx, force)
    img_url = await ctx.aget_image_urls(return_first=True)
    res = await image_safety_check(img_url)
    if res.suggest_block():
        raise ReplyException(f"图片审核结果: {res.message}")
    img = await download_image(img_url)
    await set_profile_bg_settings(ctx, image=img, force=force)

    msg = f"背景设置成功，使用\"/{ctx.region}调整个人信息\"可以调整界面方向、模糊、透明度\n"
    if res.suggest_review():
        msg += f"图片审核结果: {res.message}"
        logger.warning(f"用户 {ctx.user_id} 上传的个人信息背景图片需要人工审核: {res.message}")
        review_log_path = f"{SEKAI_PROFILE_DIR}/profile_bg_review.log"
        with open(review_log_path, 'a', encoding='utf-8') as f:
            f.write(f"{datetime.now().isoformat()} {ctx.user_id} set {ctx.region} {uid}\n")

    try:
        img_cq = await get_image_cq(
            await compose_profile_image(ctx, await get_basic_profile(ctx, uid)),
            low_quality=True,
        )
        msg = img_cq + msg.strip()
    except Exception as e:
        logger.print_exc(f"绘制个人信息背景图片失败: {get_exc_desc(e)}")
        msg += f"绘制个人信息背景图片失败: {get_exc_desc(e)}"

    return await ctx.asend_reply_msg(msg)


# 清空个人信息背景图片
clear_profile_bg = SekaiCmdHandler([
    "/pjsk clear profile bg", "/pjsk clear profile background",
    "/清空个人信息背景", "/清除个人信息背景",  "/清空个人信息图片", "/清除个人信息图片", 
])
clear_profile_bg.check_cdrate(cd).check_wblist(gbl)
@clear_profile_bg.handle()
async def _(ctx: SekaiHandlerContext):
    await ctx.block_region(key=str(ctx.user_id))

    args = ctx.get_args().strip()
    force = False
    if 'force' in args and check_superuser(ctx.event):
        force = True
        args = args.replace('force', '').strip()

    await set_profile_bg_settings(ctx, remove_image=True, force=force)
    return await ctx.asend_reply_msg(f"已清空{get_region_name(ctx.region)}个人信息背景图片")


# 调整个人信息背景设置
adjust_profile_bg = SekaiCmdHandler([
    "/pjsk adjust profile", "/pjsk adjust profile bg", "/pjsk adjust profile background",
    "/调整个人信息背景", "/调整个人信息", "/设置个人信息", "/设置个人信息背景",
])
adjust_profile_bg.check_cdrate(cd).check_wblist(gbl)
@adjust_profile_bg.handle()
async def _(ctx: SekaiHandlerContext):
    await ctx.block_region(key=str(ctx.user_id))

    args = ctx.get_args().strip()
    force = False
    if 'force' in args and check_superuser(ctx.event):
        force = True
        args = args.replace('force', '').strip()

    uid = await get_uid_and_check_verified(ctx, force)
    HELP = f"""
调整横屏/竖屏:
{ctx.original_trigger_cmd} 竖屏
调整界面模糊度(0为无模糊):
{ctx.original_trigger_cmd} 模糊 0~10
调整界面透明度(0为不透明):
{ctx.original_trigger_cmd} 透明 0~100
""".strip()
    
    args = ctx.get_args().strip()
    if not args:
        settings = get_profile_bg_settings(ctx)
        msg = f"当前{get_region_name(ctx.region)}个人信息背景设置:\n"
        msg += f"ID: {process_hide_uid(ctx, uid, keep=6)}\n"
        msg += f"方向: {'竖屏' if settings.vertical else '横屏'}\n"
        msg += f"模糊度: {settings.blur}\n"
        msg += f"透明度: {100 - int(settings.alpha * 100 // 255)}\n"
        msg += f"---\n"
        msg += HELP
        return await ctx.asend_reply_msg(msg.strip())

    vertical, blur, alpha = None, None, None
    try:
        args = args.replace('度', '').replace('%', '')

        for keyword in PROFILE_HORIZONTAL_KEYWORDS:
            if keyword in args:
                vertical = False
                args = args.replace(keyword, '', 1).strip()
                break
        for keyword in PROFILE_VERTICAL_KEYWORDS:
            if keyword in args:
                vertical = True
                args = args.replace(keyword, '', 1).strip()
                break

        if '全模糊' in args:
            blur = 10
        elif '无模糊' in args or '不模糊' in args:
            blur = 0
        elif '模糊' in args:
            numarg = args.split('模糊')[1].strip()
            num = ''
            for c in numarg:
                if c.isdigit():
                    num += c
                elif num:
                    break
            blur = int(num)

        if '不透明' in args:
            alpha = 255
        elif '全透明' in args:
            alpha = 0
        elif '透明' in args:
            numarg = args.split('透明')[1].strip()
            num = ''
            for c in numarg:
                if c.isdigit():
                    num += c
                elif num:
                    break
            alpha = (100 - int(num)) * 255 // 100
    except:
        raise ReplyException(HELP)
    
    if blur is not None:
        assert_and_reply(0 <= blur <= 10, "模糊度必须在0到10之间")
    if alpha is not None:
        assert_and_reply(0 <= alpha <= 255, "透明度必须在0到100之间")
    
    await set_profile_bg_settings(ctx, vertical=vertical, blur=blur, alpha=alpha, force=force)
    settings = get_profile_bg_settings(ctx)

    msg = f"当前设置: {'竖屏' if settings.vertical else '横屏'} 透明度{100 - int(settings.alpha * 100 / 255)} 模糊度{settings.blur}\n"

    try:
        img_cq = await get_image_cq(
            await compose_profile_image(ctx, await get_basic_profile(ctx, uid)),
            low_quality=True,
        )
        msg = img_cq + msg.strip()
    except Exception as e:
        logger.print_exc(f"绘制个人信息背景图片失败: {get_exc_desc(e)}")
        msg += f"绘制个人信息背景图片失败: {get_exc_desc(e)}"
    return await ctx.asend_reply_msg(msg.strip())


# 查询用户统计
pjsk_user_sta = CmdHandler([
    "/pjsk user sta", "/用户统计",
], logger)
pjsk_user_sta.check_cdrate(cd).check_wblist(gbl).check_superuser()
@pjsk_user_sta.handle()
async def _(ctx: HandlerContext):
    args = ctx.get_args().strip()
    group_mode = False
    detail_mode = False
    if '群' in args or 'group' in args:
        group_mode = True
    if '详细' in args or 'detail' in args:
        detail_mode = True
    bind_list: Dict[str, Dict[str, str | list[str]]] = _get_bind_list_snapshot()
    suite_total, mysekai_total, qid_set = 0, 0, set()
    suite_source_total: dict[str, int] = {}
    mysekai_source_total: dict[str, int] = {}

    msg = "所有群聊统计:\n" if not group_mode else "当前群聊统计:\n"
    group_qids = set([str(m['user_id']) for m in await get_group_users(ctx.bot, ctx.group_id)])

    for region in ALL_SERVER_REGIONS:
        qids = set(bind_list.get(region, {}).keys())
        uids = set()
        if group_mode:
            qids = qids.intersection(group_qids)
            for qid in qids:
                for uid in to_list(bind_list.get(region, {}).get(qid, [])):
                    uids.add(uid)
        qid_set.update(qids)

        suites = [
            s for s in glob.glob(config.get("suite_path").format(region=region))
            if os.path.isfile(s) and not is_aux_user_data_file(s)
        ]
        if group_mode:
            suites = [s for s in suites if get_uid_from_user_data_file(s) in uids]
        suite_total += len(suites)

        mysekais = [
            m for m in glob.glob(config.get("mysekai_path").format(region=region))
            if os.path.isfile(m) and not is_aux_user_data_file(m)
        ]
        if group_mode:
            mysekais = [m for m in mysekais if get_uid_from_user_data_file(m) in uids]
        mysekai_total += len(mysekais)

        msg += f"【{get_region_name(region)}】\n绑定 {len(qids)} | Suite {len(suites)} | MySekai {len(mysekais)}\n"

        if detail_mode:
            suite_source_num: dict[str, int] = {}
            mysekai_source_num: dict[str, int] = {}
            def get_detail():
                for p in suites:
                    local_source = get_user_data_local_source(p)
                    suite_source_num[local_source] = suite_source_num.get(local_source, 0) + 1
                for k, v in suite_source_num.items():
                    suite_source_total[k] = suite_source_total.get(k, 0) + v
                for p in mysekais:
                    local_source = get_user_data_local_source(p)
                    mysekai_source_num[local_source] = mysekai_source_num.get(local_source, 0) + 1
                for k, v in mysekai_source_num.items():
                    mysekai_source_total[k] = mysekai_source_total.get(k, 0) + v
            await run_in_pool(get_detail)
            msg += "Suite来源: " + " | ".join([f"{k} {v}" for k, v in suite_source_num.items()]) + "\n"
            msg += "MySekai来源: " + " | ".join([f"{k} {v}" for k, v in mysekai_source_num.items()]) + "\n"


    msg += f"---\n【总计】\n绑定 {len(qid_set)} | Suite {suite_total} | MySekai {mysekai_total}"
    if detail_mode:
        msg += "\nSuite来源: " + " | ".join([f"{k} {v}" for k, v in suite_source_total.items()])
        msg += "\nMySekai来源: " + " | ".join([f"{k} {v}" for k, v in mysekai_source_total.items()])

    return await ctx.asend_fold_msg_adaptive(msg.strip())


# 查询绑定历史
pjsk_bind_history = CmdHandler([
    "/pjsk bind history", "/pjsk bind his", "/绑定历史", "/绑定记录",
], logger, priority=1)
pjsk_bind_history.check_cdrate(cd).check_wblist(gbl).check_superuser()
@pjsk_bind_history.handle()
async def _(ctx: HandlerContext):
    args = ctx.get_args().strip()
    uid = None
    for region in ALL_SERVER_REGIONS:
        if validate_uid(SekaiHandlerContext.from_region(region), args):
            uid = args
            break

    if not uid:
        if ats := ctx.get_at_qids():
            qid = str(ats[0])
        else:
            qid = args

    bind_history = bind_history_db.get("history", {})
    if uid:
        # 游戏ID查QQ号
        has_any = False
        msg = f"当前绑定游戏ID{uid}的QQ用户:\n"
        bind_list_snapshot = _get_bind_list_snapshot()
        for region in ALL_SERVER_REGIONS:
            bind_list: Dict[str, str | list[str]] = bind_list_snapshot.get(region, {})
            for qid, items in bind_list.items():
                if uid in to_list(items):
                    msg += f"{qid}\n"
                    has_any = True
        if not has_any:
            msg += "无\n"

        has_any = False
        msg += f"曾经绑定过{uid}的QQ用户:\n"
        for qid, items in bind_history.items():
            for item in items:
                if item['uid'] == uid:
                    time = datetime.fromtimestamp(item['time'] / 1000).strftime('%Y-%m-%d %H:%M:%S')
                    msg += f"[{time}] {qid}"
                    has_any = True
        if not has_any:
            msg += "无\n"
            
    else:
        # QQ号查游戏ID
        has_any = False
        msg = f"用户{qid}当前绑定:\n"
        for region in ALL_SERVER_REGIONS:
            region_ctx = SekaiHandlerContext.from_region(region)
            main_uid = get_player_bind_id(region_ctx, qid, check_bind=False)
            lines = []
            for i in range(get_player_bind_count(region_ctx, qid)):
                uid = get_player_bind_id(region_ctx, qid, index=i)
                is_main = (uid == main_uid)
                line = f"[{i+1}] {uid}"
                if is_main:
                    line = "*" + line
                lines.append(line)
            if lines:
                has_any = True
                msg += f"【{get_region_name(region)}】\n" + '\n'.join(lines) + '\n'
        if not has_any:
            msg += "无\n"

        has_any = False
        msg += f"用户{qid}的绑定历史:\n"
        items = bind_history.get(qid, [])
        for item in items:
            time = datetime.fromtimestamp(item['time'] / 1000).strftime('%Y-%m-%d %H:%M:%S')
            msg += f"[{time}]\n{item['region']} {item['uid']}\n"
            has_any = True
        if not has_any:
            msg += "无\n"

    return await ctx.asend_fold_msg_adaptive(msg.strip())


# 创建游客账号
pjsk_create_guest_account = SekaiCmdHandler([
    "/pjsk create guest", "/pjsk register", "/pjsk注册",
], regions=['jp', 'en'])
guest_account_create_rate_limit = RateLimit(file_db, logger, 2, 'd', rate_limit_name='注册游客账号')
pjsk_create_guest_account.check_cdrate(cd).check_wblist(gbl).check_cdrate(guest_account_create_rate_limit)
@pjsk_create_guest_account.handle()
async def _(ctx: SekaiHandlerContext):
    region_name = get_region_name(ctx.region)
    url = get_gameapi_config(ctx).create_account_api_url
    assert_and_reply(url, f"不支持注册{region_name}帐号")
    data = await request_gameapi(url, method="POST")
    return await ctx.asend_fold_msg([
        f"注册{region_name}帐号成功，引继码和引继密码如下，登陆后请及时重新生成引继码",
        data['inherit_id'],
        data['inherit_pw'],
    ])

