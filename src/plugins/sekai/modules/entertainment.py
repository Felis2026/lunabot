from ...utils import *
from ...record import before_record_hook
from ..common import *
from ..handler import *
from ..asset import *
from ..draw import *
from .music import (
    search_music, 
    MusicSearchOptions, 
    MusicSearchResult, 
    extract_diff, 
    get_music_diff_info,
)
from .chart import generate_music_chart
from .card import (
    get_card_image, 
    has_after_training, 
    only_has_after_training, 
    get_character_name_by_id, 
    get_unit_by_card_id,
)
from .gacha import (
    spin_gacha,
    parse_search_gacha_args,
    compose_gacha_spin_image,
    SINGLE_GACHA_HELP,
)
from PIL.Image import Transpose
from PIL import ImageOps
import pydub


# ================================ 娱乐额度配置与兼容迁移 ================================ #

# 旧版只有群总限额，默认值已经在现网运行，因此继续沿用 200 作为群限制默认值。
DEFAULT_ENTERTAINMENT_DAILY_LIMIT = 200
DEFAULT_ENTERTAINMENT_DAILY_USER_LIMIT = 10

ENTERTAINMENT_LIMIT_CONFIG_KEY = "entertainment_limit_config"
ENTERTAINMENT_LIMIT_USAGE_KEY = "entertainment_limit_usage"
ENTERTAINMENT_LEGACY_LIMITS_KEY = "entertainment_daily_limits"
ENTERTAINMENT_LEGACY_USAGES_KEY = "entertainment_daily_usages"
ENTERTAINMENT_LEGACY_DATE_KEY = "entertainment_usage_date"

ENTERTAINMENT_MODE_GROUP = "group"
ENTERTAINMENT_MODE_USER = "user"
ENTERTAINMENT_MODE_HYBRID = "hybrid"
ENTERTAINMENT_MODE_SHOW_NAMES = {
    ENTERTAINMENT_MODE_GROUP: "群限制",
    ENTERTAINMENT_MODE_USER: "个人限制",
    ENTERTAINMENT_MODE_HYBRID: "双限制",
}
ENTERTAINMENT_MODE_ALIASES = {
    "group": ENTERTAINMENT_MODE_GROUP,
    "群": ENTERTAINMENT_MODE_GROUP,
    "群聊": ENTERTAINMENT_MODE_GROUP,
    "群限制": ENTERTAINMENT_MODE_GROUP,
    "群聊限制": ENTERTAINMENT_MODE_GROUP,
    "user": ENTERTAINMENT_MODE_USER,
    "个人": ENTERTAINMENT_MODE_USER,
    "个人限制": ENTERTAINMENT_MODE_USER,
    "hybrid": ENTERTAINMENT_MODE_HYBRID,
    "双限制": ENTERTAINMENT_MODE_HYBRID,
    "混合": ENTERTAINMENT_MODE_HYBRID,
    "混合限制": ENTERTAINMENT_MODE_HYBRID,
    "个人群聊": ENTERTAINMENT_MODE_HYBRID,
    "个人+群聊": ENTERTAINMENT_MODE_HYBRID,
    "个人+群限制": ENTERTAINMENT_MODE_HYBRID,
}
ENTERTAINMENT_FIELD_ALIASES = {
    "group": "group_limit",
    "群": "group_limit",
    "群聊": "group_limit",
    "群限制": "group_limit",
    "user": "user_limit",
    "个人": "user_limit",
    "个人限制": "user_limit",
}


@dataclass
class EntertainmentLimitConfig:
    enabled: bool = True
    mode: str = ENTERTAINMENT_MODE_GROUP
    group_limit: int = DEFAULT_ENTERTAINMENT_DAILY_LIMIT
    user_limit: int = DEFAULT_ENTERTAINMENT_DAILY_USER_LIMIT


@dataclass
class EntertainmentLimitSnapshot:
    config: EntertainmentLimitConfig
    group_usage: int = 0
    user_usage: int = 0


ENTERTAINMENT_QUOTA_REASON_DISABLED = "disabled"
ENTERTAINMENT_QUOTA_REASON_USER_LIMIT = "user_limit"
ENTERTAINMENT_QUOTA_REASON_GROUP_LIMIT = "group_limit"


@dataclass
class EntertainmentQuotaCheckResult:
    """统一承载娱乐额度检查结果，便于不同功能按原因决定提示文案。"""
    snapshot: EntertainmentLimitSnapshot
    block_reason: str | None = None

    @property
    def blocked(self) -> bool:
        return self.block_reason is not None


# ================================ 抽卡专属额度提示 ================================ #

# 抽卡命中娱乐额度时，前半句走趣味文案，后半句保留真实原因和操作引导。
# 这样既能让高频娱乐功能的提示更有辨识度，也不会把真实限制规则藏起来。
GACHA_ENTERTAINMENT_BLOCK_MESSAGES: Dict[str, list[tuple[str, str]]] = {
    ENTERTAINMENT_QUOTA_REASON_USER_LIMIT: [
        (
            "你的水晶库存见底了，今天先别继续梭哈啦~",
            "你今日在本群的娱乐次数已达上限，可发送 /pec 查看状态。",
        ),
        (
            "抽卡欲望很强，但今天的次数已经抽空了。",
            "你今日在本群的娱乐次数已达上限，可发送 /pec 查看状态。",
        ),
        (
            "先停一下，这位抽卡人今天已经把额度用完啦。",
            "你今日在本群的娱乐次数已达上限，可发送 /pec 查看状态。",
        ),
    ],
    ENTERTAINMENT_QUOTA_REASON_GROUP_LIMIT: [
        (
            "本群卡池已经被大家抽冒烟了，今天先封池维护一下~",
            "本群今日娱乐次数已达上限，可发送 /pec 查看状态。",
        ),
        (
            "群友们今天手气太盛，已经把本群抽卡额度薅光了。",
            "本群今日娱乐次数已达上限，可发送 /pec 查看状态。",
        ),
        (
            "卡池过热中，今天这群先暂停营业。",
            "本群今日娱乐次数已达上限，可发送 /pec 查看状态。",
        ),
    ],
    ENTERTAINMENT_QUOTA_REASON_DISABLED: [
        (
            "这群的卡池还没开放哦~",
            "当前群娱乐功能未开启，可让群管理使用 /pel 进行设置。",
        ),
        (
            "卡池还在待机状态，这里暂时抽不了。",
            "当前群娱乐功能未开启，可让群管理使用 /pel 进行设置。",
        ),
        (
            "今天不是你手黑，是这群压根还没开池。",
            "当前群娱乐功能未开启，可让群管理使用 /pel 进行设置。",
        ),
    ],
}

GUESS_INTERVAL = timedelta(seconds=1)
HINT_KEYWORDS = ['提示']
STOP_KEYWORDS = ['结束猜', '停止猜', '结束听', '停止听']

@dataclass
class ImageRandomCropOptions:
    rate_min: float
    rate_max: float
    flip_prob: float = 0.
    inv_prob: float = 0.
    gray_prob: float = 0.
    rgb_shuffle_prob: float = 0.
    at_least_one_effect: bool = False

    def get_effect_tip_text(self):
        effects = []
        if self.flip_prob > 0: effects.append("翻转")
        if self.inv_prob > 0: effects.append("反色")
        if self.gray_prob > 0: effects.append("灰度")
        if self.rgb_shuffle_prob > 0: effects.append("RGB打乱")
        if len(effects) == 0: return ""
        return f"（概率出现{'、'.join(effects)}效果）"

@dataclass
class ChartRandomClipOptions:
    rate_min: float
    rate_max: float
    mirror_prob: float = 0.

    def get_effect_tip_text(self):
        effects = []
        if self.mirror_prob > 0: effects.append("镜像")
        if len(effects) == 0: return ""
        return f"（概率出现{'、'.join(effects)}效果）"


GUESS_COVER_TIMEOUT = timedelta(seconds=60) 
GUESS_COVER_DIFF_OPTIONS = {
    'easy':     ImageRandomCropOptions(0.4, 0.5),
    'normal':   ImageRandomCropOptions(0.3, 0.5),
    'hard':     ImageRandomCropOptions(0.2, 0.3),
    'expert':   ImageRandomCropOptions(0.1, 0.3),
    'master':   ImageRandomCropOptions(0.1, 0.15),
    'append':   ImageRandomCropOptions(0.2, 0.5, flip_prob=0.4, inv_prob=0.4, gray_prob=0.4, rgb_shuffle_prob=0.4, at_least_one_effect=True),
}

GUESS_CHART_TIMEOUT = timedelta(seconds=60)
GUESS_CHART_DIFF_OPTIONS = {
    'easy':     ChartRandomClipOptions(0.4, 0.4),
    'normal':   ChartRandomClipOptions(0.3, 0.4),
    'hard':     ChartRandomClipOptions(0.1, 0.3),
    'expert':   ChartRandomClipOptions(0.1, 0.2),
    'master':   ChartRandomClipOptions(0.05, 0.1),
}

GUESS_CARD_TIMEOUT = timedelta(seconds=60)
GUESS_CARD_DIFF_OPTIONS = {
    'easy':     ImageRandomCropOptions(0.5, 0.5),
    'normal':   ImageRandomCropOptions(0.4, 0.5),
    'hard':     ImageRandomCropOptions(0.3, 0.4),
    'expert':   ImageRandomCropOptions(0.2, 0.3),
    'master':   ImageRandomCropOptions(0.1, 0.2),
    'append':   ImageRandomCropOptions(0.2, 0.3, flip_prob=0.4, inv_prob=0.4, gray_prob=0.4, rgb_shuffle_prob=0.4, at_least_one_effect=True),
}
GUESS_CARD_CID_LIMIT = 10

GUESS_MUSIC_TIMEOUT = timedelta(seconds=60)
GUESS_MUSIC_DIFF_OPTIONS = {
    'easy':     (15.0, False),
    'normal':   (10.0, False),
    'hard':     (7.5, False),
    'expert':   (5.0, False),
    'master':   (2.0, False),
    'append':   (10.0, True),
}


# ================================ 娱乐额度公共逻辑 ================================ #

def _get_entertainment_today_str() -> str:
    """统一日期口径，避免不同入口各自拼接日期字符串。"""
    return datetime.now().strftime("%Y-%m-%d")


def _normalize_non_negative_int(value: Any, default: int) -> int:
    """将持久化里的宽松值收敛成非负整数，避免脏数据继续向外扩散。"""
    try:
        value = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, value)


def _normalize_entertainment_limit_config(raw: Any) -> EntertainmentLimitConfig:
    """读取配置时做一次归一化，避免旧字段或异常值影响运行时判断。"""
    if not isinstance(raw, dict):
        return EntertainmentLimitConfig()
    mode = str(raw.get("mode", ENTERTAINMENT_MODE_GROUP)).strip().lower()
    if mode not in ENTERTAINMENT_MODE_SHOW_NAMES:
        mode = ENTERTAINMENT_MODE_GROUP
    return EntertainmentLimitConfig(
        enabled=bool(raw.get("enabled", True)),
        mode=mode,
        group_limit=_normalize_non_negative_int(raw.get("group_limit", DEFAULT_ENTERTAINMENT_DAILY_LIMIT), DEFAULT_ENTERTAINMENT_DAILY_LIMIT),
        user_limit=_normalize_non_negative_int(raw.get("user_limit", DEFAULT_ENTERTAINMENT_DAILY_USER_LIMIT), DEFAULT_ENTERTAINMENT_DAILY_USER_LIMIT),
    )


def _serialize_entertainment_limit_config(config: EntertainmentLimitConfig) -> Dict[str, Any]:
    """把运行时配置收敛回稳定字典结构，便于后续兼容迁移。"""
    return {
        "enabled": bool(config.enabled),
        "mode": str(config.mode),
        "group_limit": int(config.group_limit),
        "user_limit": int(config.user_limit),
    }


def _ensure_mode_limit_defaults(config: EntertainmentLimitConfig, mode: str | None = None) -> EntertainmentLimitConfig:
    """切模式时补齐必要的默认值，避免旧版 0 次配置把新模式卡死。"""
    target_mode = mode or config.mode
    if target_mode in (ENTERTAINMENT_MODE_GROUP, ENTERTAINMENT_MODE_HYBRID) and config.group_limit <= 0:
        config.group_limit = DEFAULT_ENTERTAINMENT_DAILY_LIMIT
    if target_mode in (ENTERTAINMENT_MODE_USER, ENTERTAINMENT_MODE_HYBRID) and config.user_limit <= 0:
        config.user_limit = DEFAULT_ENTERTAINMENT_DAILY_USER_LIMIT
    return config


def _normalize_usage_entry(raw: Any) -> Dict[str, Any]:
    """归一化单个群的使用记录，防止旧数据或异常值破坏配额统计。"""
    if not isinstance(raw, dict):
        return {"group_total": 0, "users": {}}
    users: Dict[str, int] = {}
    raw_users = raw.get("users", {})
    if isinstance(raw_users, dict):
        for uid, count in raw_users.items():
            normalized = _normalize_non_negative_int(count, 0)
            if normalized > 0:
                users[str(uid)] = normalized
    return {
        "group_total": _normalize_non_negative_int(raw.get("group_total", 0), 0),
        "users": users,
    }


def _migrate_legacy_entertainment_storage() -> tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]:
    """兼容旧版群总限额结构，首次读取时迁移到新结构。"""
    legacy_limits = file_db.get(ENTERTAINMENT_LEGACY_LIMITS_KEY, {})
    legacy_usages = file_db.get(ENTERTAINMENT_LEGACY_USAGES_KEY, {})
    legacy_date = file_db.get(ENTERTAINMENT_LEGACY_DATE_KEY, None)

    config_map: Dict[str, Dict[str, Any]] = {}
    group_ids = set()
    if isinstance(legacy_limits, dict):
        group_ids.update(str(group_id) for group_id in legacy_limits.keys())
    if isinstance(legacy_usages, dict):
        group_ids.update(str(group_id) for group_id in legacy_usages.keys())

    for group_id in group_ids:
        limit = DEFAULT_ENTERTAINMENT_DAILY_LIMIT
        if isinstance(legacy_limits, dict):
            limit = _normalize_non_negative_int(legacy_limits.get(group_id, DEFAULT_ENTERTAINMENT_DAILY_LIMIT), DEFAULT_ENTERTAINMENT_DAILY_LIMIT)
        config_map[str(group_id)] = _serialize_entertainment_limit_config(EntertainmentLimitConfig(
            enabled=limit > 0,
            mode=ENTERTAINMENT_MODE_GROUP,
            group_limit=limit,
            user_limit=DEFAULT_ENTERTAINMENT_DAILY_USER_LIMIT,
        ))

    usage_groups: Dict[str, Dict[str, Any]] = {}
    today = _get_entertainment_today_str()
    if legacy_date == today and isinstance(legacy_usages, dict):
        for group_id, count in legacy_usages.items():
            normalized = _normalize_non_negative_int(count, 0)
            if normalized > 0:
                usage_groups[str(group_id)] = {"group_total": normalized, "users": {}}

    return config_map, {"date": today, "groups": usage_groups}


def _load_entertainment_limit_storage() -> tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]:
    """集中读取并规范化娱乐配置与使用量，所有入口统一走这里。"""
    raw_config = file_db.get(ENTERTAINMENT_LIMIT_CONFIG_KEY, None)
    raw_usage = file_db.get(ENTERTAINMENT_LIMIT_USAGE_KEY, None)
    need_save = False

    if raw_config is None and raw_usage is None:
        raw_config, raw_usage = _migrate_legacy_entertainment_storage()
        need_save = bool(raw_config) or bool(raw_usage.get("groups"))

    if not isinstance(raw_config, dict):
        raw_config = {}
        need_save = True
    if not isinstance(raw_usage, dict):
        raw_usage = {}
        need_save = True

    config_map = {
        str(group_id): _serialize_entertainment_limit_config(_normalize_entertainment_limit_config(config))
        for group_id, config in raw_config.items()
    }
    if config_map != raw_config:
        need_save = True

    today = _get_entertainment_today_str()
    raw_usage_groups = raw_usage.get("groups", {}) if raw_usage.get("date") == today and isinstance(raw_usage.get("groups", {}), dict) else {}
    usage_groups: Dict[str, Dict[str, Any]] = {}
    for group_id, usage in raw_usage_groups.items():
        normalized = _normalize_usage_entry(usage)
        if normalized["group_total"] > 0 or normalized["users"]:
            usage_groups[str(group_id)] = normalized
    usage_state = {"date": today, "groups": usage_groups}
    if usage_state != raw_usage:
        need_save = True

    if need_save:
        file_db.set(ENTERTAINMENT_LIMIT_CONFIG_KEY, config_map)
        file_db.set(ENTERTAINMENT_LIMIT_USAGE_KEY, usage_state)

    return config_map, usage_state


def _save_entertainment_limit_storage(config_map: Dict[str, Dict[str, Any]], usage_state: Dict[str, Any]) -> None:
    """统一写回娱乐限制存储，避免不同入口各自散写键值。"""
    file_db.set(ENTERTAINMENT_LIMIT_CONFIG_KEY, config_map)
    file_db.set(ENTERTAINMENT_LIMIT_USAGE_KEY, usage_state)


def _get_entertainment_limit_snapshot(group_id: int | str, user_id: int | str | None = None) -> EntertainmentLimitSnapshot:
    """读取单群视角的配置与今日使用量，供展示与扣次判断共用。"""
    config_map, usage_state = _load_entertainment_limit_storage()
    gid = str(group_id)
    uid = None if user_id is None else str(user_id)
    config = _normalize_entertainment_limit_config(config_map.get(gid, {}))
    usage = _normalize_usage_entry(usage_state.get("groups", {}).get(gid, {}))
    return EntertainmentLimitSnapshot(
        config=config,
        group_usage=usage["group_total"],
        user_usage=usage["users"].get(uid, 0) if uid is not None else 0,
    )


def _get_entertainment_reopen_hint(config: EntertainmentLimitConfig) -> str:
    """关闭状态下给出最短的恢复指令提示，减少群管试错成本。"""
    if config.mode == ENTERTAINMENT_MODE_HYBRID:
        return "发送“/pel 100 100”可重新开启并同时设置个人/群聊限制"
    return "发送“/pel 100”可重新开启并按当前模式设置限制"


def _format_entertainment_limit_status(snapshot: EntertainmentLimitSnapshot) -> str:
    """统一 `/pec` 与设置反馈的状态文案，避免不同入口展示口径漂移。"""
    config = snapshot.config
    mode_name = ENTERTAINMENT_MODE_SHOW_NAMES.get(config.mode, config.mode)
    lines = [
        f"本群娱乐功能{'已开启' if config.enabled else '未开启'}",
        f"模式：{mode_name}",
    ]
    if config.mode in (ENTERTAINMENT_MODE_GROUP, ENTERTAINMENT_MODE_HYBRID):
        lines.append(f"群聊限制：{config.group_limit}次/日")
    if config.mode in (ENTERTAINMENT_MODE_USER, ENTERTAINMENT_MODE_HYBRID):
        lines.append(f"个人限制：{config.user_limit}次/日")
    if not config.enabled:
        lines.append(_get_entertainment_reopen_hint(config))
        return "\n".join(lines)
    if config.mode == ENTERTAINMENT_MODE_GROUP:
        lines.append(f"本群今日使用：{snapshot.group_usage}/{config.group_limit}次")
    elif config.mode == ENTERTAINMENT_MODE_USER:
        lines.append(f"你今日使用：{snapshot.user_usage}/{config.user_limit}次")
    else:
        lines.append(f"你今日使用：{snapshot.user_usage}/{config.user_limit}次")
        lines.append(f"本群今日使用：{snapshot.group_usage}/{config.group_limit}次")
    return "\n".join(lines)


def _parse_entertainment_mode_name(raw: str) -> str | None:
    normalized = str(raw).strip().lower().replace(" ", "")
    return ENTERTAINMENT_MODE_ALIASES.get(normalized)


def _parse_entertainment_field_name(raw: str) -> str | None:
    normalized = str(raw).strip().lower().replace(" ", "")
    return ENTERTAINMENT_FIELD_ALIASES.get(normalized)


def _parse_positive_limit_value(raw: str) -> int:
    """娱乐限制显式设值时要求正整数，避免和关闭语义混淆。"""
    value = _normalize_non_negative_int(raw, -1)
    assert_and_reply(value > 0, "限制次数必须是大于 0 的整数；如需关闭请使用“/pel 0”")
    return value


def _get_entertainment_limit_usage_text() -> str:
    return "\n".join([
        "使用方式：",
        "/pel 0",
        "/pel 100",
        "/pel 100 100",
        "/pel 模式 群限制|个人限制|双限制",
        "/pel 群 200",
        "/pel 个人 10",
    ])


def _as_list(value: Any) -> list[Any]:
    """启动消息既可能是单条，也可能是多条，统一收口成列表处理。"""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _ensure_group_entertainment_context(ctx: SekaiHandlerContext, feature_name: str) -> None:
    """娱乐限制是群维度配置，群命令在私聊里直接给出明确提示。"""
    assert_and_reply(is_group_msg(ctx.event), f"{feature_name}请在群内使用")


async def _is_group_admin_or_superuser(ctx: SekaiHandlerContext) -> bool:
    """优先复用事件中的 role；缺失时回退查群成员信息，避免部分平台字段不全。"""
    if check_superuser(ctx.event):
        return True
    if not is_group_msg(ctx.event):
        return False
    sender = getattr(ctx.event, "sender", None)
    role = str(getattr(sender, "role", "") or "").strip().lower()
    if role in {"owner", "admin"}:
        return True
    try:
        info = await ctx.bot.call_api(
            "get_group_member_info",
            group_id=int(ctx.group_id),
            user_id=int(ctx.user_id),
            no_cache=True,
        )
    except Exception as error:
        logger.warning(f"获取群成员权限信息失败，gid={ctx.group_id} uid={ctx.user_id}: {get_exc_desc(error)}")
        return False
    return str(info.get("role", "") or "").strip().lower() in {"owner", "admin"}


# ================================ 娱乐额度拦截与提示 ================================ #

def _get_group_entertainment_quota_check_result(ctx: SekaiHandlerContext) -> EntertainmentQuotaCheckResult:
    """
    统一产出群娱乐额度检查结果。

    规则判断和文案格式化拆开后，抽卡等高频功能就能复用同一套判断逻辑，
    只替换展示文案，不会再出现“为了换提示文案去复制限额判断”的问题。
    """
    _ensure_group_entertainment_context(ctx, "该娱乐功能")
    snapshot = _get_entertainment_limit_snapshot(ctx.group_id, ctx.user_id)
    config = snapshot.config
    if not config.enabled:
        return EntertainmentQuotaCheckResult(snapshot=snapshot, block_reason=ENTERTAINMENT_QUOTA_REASON_DISABLED)
    if config.mode in (ENTERTAINMENT_MODE_USER, ENTERTAINMENT_MODE_HYBRID) and snapshot.user_usage >= config.user_limit:
        return EntertainmentQuotaCheckResult(snapshot=snapshot, block_reason=ENTERTAINMENT_QUOTA_REASON_USER_LIMIT)
    if config.mode in (ENTERTAINMENT_MODE_GROUP, ENTERTAINMENT_MODE_HYBRID) and snapshot.group_usage >= config.group_limit:
        return EntertainmentQuotaCheckResult(snapshot=snapshot, block_reason=ENTERTAINMENT_QUOTA_REASON_GROUP_LIMIT)
    return EntertainmentQuotaCheckResult(snapshot=snapshot)


def _format_default_entertainment_quota_block_message(result: EntertainmentQuotaCheckResult) -> str:
    """默认娱乐功能提示继续沿用原有口径，避免影响非抽卡功能的用户预期。"""
    assert result.blocked, "只有命中限制时才需要格式化拦截提示"
    config = result.snapshot.config
    if result.block_reason == ENTERTAINMENT_QUOTA_REASON_DISABLED:
        return "本群娱乐功能未开启"
    if result.block_reason == ENTERTAINMENT_QUOTA_REASON_USER_LIMIT:
        return f"你今日在本群的娱乐功能使用次数已达上限{config.user_limit}次"
    if result.block_reason == ENTERTAINMENT_QUOTA_REASON_GROUP_LIMIT:
        return f"本群今日娱乐功能使用次数已达上限{config.group_limit}次"
    return "当前娱乐功能暂时不可用"


def _format_gacha_entertainment_quota_block_message(result: EntertainmentQuotaCheckResult) -> str:
    """抽卡命中额度时使用专属文案，兼顾趣味性与真实原因说明。"""
    assert result.blocked, "只有命中限制时才需要格式化拦截提示"
    candidates = GACHA_ENTERTAINMENT_BLOCK_MESSAGES.get(result.block_reason, [])
    if not candidates:
        return _format_default_entertainment_quota_block_message(result)
    title, detail = random.choice(candidates)
    return f"{title}\n{detail}"


def _check_group_entertainment_quota_available(
    ctx: SekaiHandlerContext,
    block_message_formatter=None,
) -> EntertainmentLimitSnapshot:
    """在发送结果前先做只读校验，避免超限时已经把内容发出去了。"""
    result = _get_group_entertainment_quota_check_result(ctx)
    if result.blocked:
        formatter = block_message_formatter or _format_default_entertainment_quota_block_message
        raise ReplyException(formatter(result))
    return result.snapshot


def _commit_group_entertainment_quota_usage(ctx: SekaiHandlerContext) -> EntertainmentLimitSnapshot:
    """在成功发送启动/结果消息后再记次，减少参数错误和内部异常导致的误扣。"""
    config_map, usage_state = _load_entertainment_limit_storage()
    gid = str(ctx.group_id)
    uid = str(ctx.user_id)
    config = _normalize_entertainment_limit_config(config_map.get(gid, {}))
    if not config.enabled:
        return _get_entertainment_limit_snapshot(gid, uid)
    usage = usage_state["groups"].setdefault(gid, {"group_total": 0, "users": {}})
    usage = _normalize_usage_entry(usage)
    if config.mode in (ENTERTAINMENT_MODE_GROUP, ENTERTAINMENT_MODE_HYBRID):
        usage["group_total"] += 1
    if config.mode in (ENTERTAINMENT_MODE_USER, ENTERTAINMENT_MODE_HYBRID):
        usage["users"][uid] = usage["users"].get(uid, 0) + 1
    usage_state["groups"][gid] = usage
    _save_entertainment_limit_storage(config_map, usage_state)
    return _get_entertainment_limit_snapshot(gid, uid)


def _update_group_entertainment_limit_config(group_id: int | str, update_fn) -> EntertainmentLimitConfig:
    """集中修改单群配置，避免不同命令分散地直接改 file_db 键值。"""
    config_map, usage_state = _load_entertainment_limit_storage()
    gid = str(group_id)
    config = _normalize_entertainment_limit_config(config_map.get(gid, {}))
    updated = update_fn(config)
    if updated is not None:
        config = updated
    config_map[gid] = _serialize_entertainment_limit_config(config)
    _save_entertainment_limit_storage(config_map, usage_state)
    return _normalize_entertainment_limit_config(config_map[gid])

@dataclass
class GuessContext:
    ctx: SekaiHandlerContext
    guess_type: str
    scope_key: str
    scope_desc: str
    group_id: Optional[int] = None
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    user_id: Optional[int] = None
    msg_id: Optional[int] = None
    text: Optional[str] = None
    guess_success: bool = False
    used_hint_types: Set[str] = field(default_factory=set)
    data: Dict[str, Any] = field(default_factory=dict)

    async def asend_msg(self, msg: str):
        return await self.ctx.asend_msg(msg)
    
    async def asend_reply_msg(self, msg: str):
        msg = f"[CQ:reply,id={self.msg_id}]{msg}"
        return await self.ctx.asend_msg(msg)


# ================================ 猜题会话作用域 ================================ #

def _get_guess_scope_key(event_or_ctx: MessageEvent | SekaiHandlerContext) -> str:
    """
    统一群聊/私聊的猜题会话键。

    `/pel` 与 `/pec` 仍然只管群聊额度；猜曲绘/猜谱面/猜卡面/听歌识曲本身则允许在私聊独立开局。
    """
    event = event_or_ctx.event if isinstance(event_or_ctx, SekaiHandlerContext) else event_or_ctx
    if is_group_msg(event):
        return f"g:{event.group_id}"
    return f"p:{event.user_id}"


def _get_guess_scope_desc(event_or_ctx: MessageEvent | SekaiHandlerContext) -> str:
    """生成日志描述，避免私聊场景继续误写成“群聊 xxx”。"""
    event = event_or_ctx.event if isinstance(event_or_ctx, SekaiHandlerContext) else event_or_ctx
    if is_group_msg(event):
        return f"群聊 {event.group_id}"
    return f"私聊 {event.user_id}"


guess_resp_queues: Dict[str, Dict[str, asyncio.Queue[MessageEvent]]] = {}
guess_user_last_reply_time: Dict[tuple[str, int], datetime] = {}

# 记录当前猜x的消息事件
@before_record_hook
async def get_guess_resp_event(bot: Bot, event: MessageEvent):
    if event.user_id == int(bot.self_id): return
    if check_in_blacklist(event.user_id): return
    if event.get_plaintext().startswith("/"): return
    scope_key = _get_guess_scope_key(event)
    queues = guess_resp_queues.get(scope_key, {})
    for q in queues.values():
        q.put_nowait(event)

# 开始猜x
async def start_guess(ctx: SekaiHandlerContext, guess_type: str, timeout: timedelta, start_fn, check_fn, stop_fn, hint_fn):
    scope_key = _get_guess_scope_key(ctx)
    scope_desc = _get_guess_scope_desc(ctx)
    group_id = ctx.group_id if is_group_msg(ctx.event) else None
    current_guesses = list(guess_resp_queues.get(scope_key, {}).keys())
    current_guess = current_guesses[0] if len(current_guesses) > 0 else None
    assert_and_reply(not current_guess, f"当前{current_guess}正在进行")
    await ctx.block(f"guess_{scope_key}", timeout=0)

    if scope_key not in guess_resp_queues:
        guess_resp_queues[scope_key] = {}
    guess_resp_queues[scope_key][guess_type] = asyncio.Queue()

    try:
        logger.info(f"{scope_desc} 开始{guess_type}，timeout={timeout.total_seconds()}s")

        # ================================ 启动阶段 ================================ #
        # 先完成题目素材准备，再在群聊场景校验/扣减娱乐次数。
        # 这样能保持原来的“只有启动消息真正发出去才记次”的语义，同时不影响私聊可用性。
        gctx = GuessContext(
            ctx=ctx, 
            guess_type=guess_type, 
            scope_key=scope_key,
            scope_desc=scope_desc,
            group_id=group_id,
        )
        startup_msgs = _as_list(await start_fn(gctx))
        if group_id is not None:
            _check_group_entertainment_quota_available(ctx)
        for msg in startup_msgs:
            await gctx.asend_msg(msg)
        if group_id is not None:
            _commit_group_entertainment_quota_usage(ctx)

        gctx.start_time = datetime.now()
        gctx.end_time = datetime.now() + timeout

        # ================================ 收集回答 ================================ #
        # 同一用户在同一会话作用域内保留原有 1 秒节流，避免群聊和私聊之间互相抢冷却。
        while True:
            try:
                rest_time = gctx.end_time - datetime.now()
                if rest_time.total_seconds() <= 0:
                    raise asyncio.TimeoutError
                event = await asyncio.wait_for(
                    guess_resp_queues[scope_key][guess_type].get(), 
                    timeout=rest_time.total_seconds()
                )
                uid, mid, text = event.user_id, event.message_id, event.get_plaintext()
                time = datetime.fromtimestamp(event.time)
                guess_user_key = (scope_key, uid)
                if time - guess_user_last_reply_time.get(guess_user_key, datetime.min) < GUESS_INTERVAL:
                    continue
                guess_user_last_reply_time[guess_user_key] = time
                # logger.info(f"{scope_desc} 收到{guess_type}消息: uid={uid}, text={text}")

                gctx.user_id = uid
                gctx.msg_id = mid
                gctx.text = text

                if any([kw in text for kw in HINT_KEYWORDS]):
                    await hint_fn(gctx)
                    continue

                if any([kw in text for kw in STOP_KEYWORDS]):
                    await stop_fn(gctx)
                    return

                await check_fn(gctx)
                if gctx.guess_success:
                    break

            except asyncio.TimeoutError:
                await stop_fn(gctx)
                return
    finally:
        logger.info(f"{scope_desc} 停止{guess_type}")
        if scope_key in guess_resp_queues and guess_type in guess_resp_queues[scope_key]:
            del guess_resp_queues[scope_key][guess_type]
        if scope_key in guess_resp_queues and not guess_resp_queues[scope_key]:
            del guess_resp_queues[scope_key]

# 随机裁剪图片到 w=[w*rate_min, w*rate_max], h=[h*rate_min, h*rate_max]
async def random_crop_image(image: Image.Image, options: ImageRandomCropOptions) -> Image.Image:
    image = image.convert("RGB")
    w, h = image.size
    w_rate = random.uniform(options.rate_min, options.rate_max)
    h_rate = random.uniform(options.rate_min, options.rate_max)
    w_crop = int(w * w_rate)
    h_crop = int(h * h_rate)
    x = random.randint(0, w - w_crop)
    y = random.randint(0, h - h_crop)
    ret = image.crop((x, y, x + w_crop, y + h_crop))

    flip, inv, gray, rgb_shuffle = False, False, False, False
    while True:
        flip = random.random() < options.flip_prob
        inv = random.random() < options.inv_prob
        gray = random.random() < options.gray_prob
        rgb_shuffle = random.random() < options.rgb_shuffle_prob
        if not options.at_least_one_effect or any([flip, inv, gray, rgb_shuffle]):
            break
        
    if flip:
        if random.random() < 0.5:
            ret = ret.transpose(Transpose.FLIP_LEFT_RIGHT)
        else:
            ret = ret.transpose(Transpose.FLIP_TOP_BOTTOM)
    if inv:
        ret = ImageOps.invert(ret)
    if gray:
        ret = ImageOps.grayscale(ret).convert("RGB")
    if rgb_shuffle:
        channels = list(range(3))
        random.shuffle(channels)
        ret = ret.split()
        ret = Image.merge("RGB", (ret[channels[0]], ret[channels[1]], ret[channels[2]]))
    return ret

# 随机歌曲，返回（歌曲数据，封面缩略图cq码，资源类型）
@retry(stop=stop_after_attempt(3), reraise=True)
async def random_music(ctx: SekaiHandlerContext, res_type: str) -> Tuple[Dict, Image.Image, Any]:
    assert res_type in ['cover', 'audio']
    musics = await ctx.md.musics.get()
    music = random.choice(musics)
    assert datetime.now() > datetime.fromtimestamp(music['publishedAt'] / 1000)
    asset_name = music['assetbundleName']
    cover_img = await ctx.rip.img(f"music/jacket/{asset_name}_rip/{asset_name}.png", allow_error=False)
    cover_thumb_cq = await get_image_cq(cover_img.resize((200, 200)), low_quality=True)
    if res_type == 'cover':
        return music, cover_thumb_cq, cover_img.resize((512, 512))
    elif res_type == 'audio':
        # 随机一个版本音频
        vocals = await ctx.md.music_vocals.find_by('musicId', music['id'], mode='all')
        vocal_assetname = random.choice(vocals)['assetbundleName']
        audio_path = await ctx.rip.get_asset_cache_path(f"music/long/{vocal_assetname}/{vocal_assetname}.mp3")
        return music, cover_thumb_cq, audio_path

# 发送猜曲提示
async def send_guess_music_hint(gctx: GuessContext):
    music = gctx.data['music']
    music_diff = await get_music_diff_info(gctx.ctx, music['id'])

    hint_types = ['ma_diff', 'title_first', 'title_last']
    if music_diff.has_append: hint_types.append('apd_diff')
    hint_types = [t for t in hint_types if t not in gctx.used_hint_types] 
    if len(hint_types) == 0:
        await gctx.asend_reply_msg("没有更多提示了！")
        return
    hint_type = random.choice(hint_types)

    msg = f"提示："
    if hint_type == 'title_first':
        msg += f"歌曲标题以\"{music['title'][0]}\"开头"
    elif hint_type == 'title_last':
        msg += f"歌曲标题以\"{music['title'][-1]}\"结尾"
    elif hint_type == 'ma_diff':
        msg += f"MASTER Lv.{music_diff.level['master']}"
    elif hint_type == 'apd_diff':
        msg += f"APPEND Lv.{music_diff.level['append']}"
    elif hint_type == 'month':
        time = datetime.fromtimestamp(music['publishedAt'] / 1000.)
        msg += f"发布时间为{time.year}年{time.month}月"

    gctx.used_hint_types.add(hint_type)    
    await gctx.asend_msg(msg)

# 获取卡面标题
async def get_card_title(ctx: SekaiHandlerContext, card: Dict, after_training: bool) -> str:
    title = f"【{card['id']}】"
    rarity = card['cardRarityType']
    if rarity == 'rarity_1': title += "⭐"
    elif rarity == 'rarity_2': title += "⭐⭐"
    elif rarity == 'rarity_3': title += "⭐⭐⭐"
    elif rarity == 'rarity_4': title += "⭐⭐⭐⭐"
    elif rarity == 'rarity_birthday': title += "🎀"
    title += " " + await get_character_name_by_id(ctx, card['characterId'])
    title += f" - {card['prefix']}"
    if rarity in ['rarity_3', 'rarity_4']:
        if after_training:  title += "（特训后）"
        else:               title += "（特训前）"
    return title

# 随机卡面，返回卡牌数据、卡面图片、是否特训
@retry(stop=stop_after_attempt(3), reraise=True)
async def random_card(ctx: SekaiHandlerContext) -> Tuple[Dict, Image.Image, str]:
    cards = await ctx.md.cards.get()
    while True:
        card = random.choice(cards)
        if datetime.fromtimestamp(card['releaseAt'] / 1000) > datetime.now():
            continue
        if card['cardRarityType'] in ['rarity_3', 'rarity_4', 'rarity_birthday']:
            break
    if not has_after_training(card):
        after_training = False
    elif only_has_after_training(card):
        after_training = True
    else:
        after_training = random.choice([True, False])
    card_img = await get_card_image(ctx, card['id'], after_training=after_training, allow_error=False)
    card_img = resize_keep_ratio(card_img, 1024 * 512, mode='wxh')
    return card, card_img, after_training

# 发送猜卡面提示
async def send_guess_card_hint(gctx: GuessContext):
    card = gctx.data['card']
    after_training = gctx.data['after_training']

    hint_types = ['name', 'rarity_and_attr', 'unit']
    hint_types = [t for t in hint_types if t not in gctx.used_hint_types]
    if len(hint_types) == 0:
        await gctx.asend_reply_msg("没有更多提示了！")
        return
    hint = random.choice(hint_types)

    msg = f"提示："
    if hint == 'name':
        msg += f"标题为\"{card['prefix']}\""
    elif hint == 'after_training':
        if after_training:  msg += "特训后"
        else:               msg += "特训前"
    elif hint == 'rarity_and_attr':
        rarity = card['cardRarityType']
        if rarity == 'rarity_1': msg += "1星"
        elif rarity == 'rarity_2': msg += "2星"
        elif rarity == 'rarity_3': msg += "3星"
        elif rarity == 'rarity_4': msg += "4星"
        elif rarity == 'rarity_birthday': msg += "生日卡"
        msg += "&"
        attr = card['attr']
        if attr == 'cool': msg += "蓝星"
        elif attr == 'happy': msg += "橙心"
        elif attr == 'mysterious': msg += "紫月"
        elif attr == 'cute': msg += "粉花"
        elif attr == 'pure': msg += "绿草"
    elif hint == 'month':
        time = datetime.fromtimestamp(card['releaseAt'] / 1000.)
        msg += f"发布时间为{time.year}年{time.month}月"
    elif hint == 'unit':
        unit = await get_unit_by_card_id(gctx.ctx, card['id'])
        if unit == 'light_sound': msg += "ln"
        elif unit == 'idol': msg += "mmj"
        elif unit == 'street': msg += "vbs"
        elif unit == 'theme_park': msg += "ws"
        elif unit == 'school_refusal': msg += "25时"
        elif unit == 'piapro': msg += "vs"

    gctx.used_hint_types.add(hint)
    await gctx.asend_msg(msg)

# 随机裁剪音频+反转
async def random_clip_audio(input_path: str, save_path: str, length: float, reverse: bool = False, clip_start=20.0, clip_end=10.0):
    audio = pydub.AudioSegment.from_file(input_path)
    length = int(length * 1000)
    start = random.randint(int(clip_start * 1000), len(audio) - length - int(clip_end * 1000))
    clip = audio[start:start + length]
    if reverse:
        clip = clip.reverse()
    clip.export(save_path, format='mp3')

# 获取猜曲检查函数
def get_guess_music_check_fn(guess_type: str):
    async def check_fn(gctx: GuessContext):
        music, cover_thumb = gctx.data['music'], gctx.data['cover_thumb']
        ret: MusicSearchResult = await search_music(
            gctx.ctx, 
            gctx.text, 
            MusicSearchOptions(
                use_id=False,
                use_nidx=False,
                use_emb=False, 
                raise_when_err=False, 
                verbose=False
            ))
        if ret.music is None:
            return
        if ret.music['id'] == music['id']:
            await gctx.asend_reply_msg(f"你猜对了！\n【{music['id']}】{music['title']}{cover_thumb}")
            gctx.guess_success = True
    return check_fn

# 获取猜曲停止函数
def get_guess_music_stop_fn(guess_type: str):
    async def stop_fn(gctx: GuessContext):
        music, cover_thumb = gctx.data['music'], gctx.data['cover_thumb']
        await gctx.asend_msg(f"{guess_type}结束，正确答案：\n【{music['id']}】{music['title']}{cover_thumb}")
    return stop_fn


# ================================ 娱乐额度指令 ================================ #

pjsk_entertainment_limit = SekaiCmdHandler([
    "/pjsk entertainment limit", "/pjsk_entertainment_limit",
    "/pjsk娱乐功能限制", "/pjsk娱乐功能上限", "/pel",
], regions=['jp'])
pjsk_entertainment_limit.check_cdrate(cd).check_wblist(gbl)
@pjsk_entertainment_limit.handle()
async def _(ctx: SekaiHandlerContext):
    _ensure_group_entertainment_context(ctx, "娱乐功能限制")
    assert_and_reply(await _is_group_admin_or_superuser(ctx), "仅群主、群管理员或 superuser 可设置本群娱乐限制")

    snapshot = _get_entertainment_limit_snapshot(ctx.group_id, ctx.user_id)
    args = ctx.get_args().strip()
    if not args:
        return await ctx.asend_reply_msg(f"{_format_entertainment_limit_status(snapshot)}\n{_get_entertainment_limit_usage_text()}")

    parts = args.split()

    # ================================ 关闭与模式切换 ================================ #
    # `/pel 0` 作为统一关闭语义；模式切换只调整配置，不隐式重新开启功能。
    if len(parts) == 1 and parts[0] == "0":
        config = _update_group_entertainment_limit_config(ctx.group_id, lambda config: EntertainmentLimitConfig(
            enabled=False,
            mode=config.mode,
            group_limit=config.group_limit,
            user_limit=config.user_limit,
        ))
        status = _format_entertainment_limit_status(_get_entertainment_limit_snapshot(ctx.group_id, ctx.user_id))
        return await ctx.asend_reply_msg(f"已关闭本群娱乐功能\n{status}")

    if len(parts) == 2 and parts[0].strip().lower() in {"mode", "模式"}:
        mode = _parse_entertainment_mode_name(parts[1])
        assert_and_reply(mode is not None, "可选模式：群限制 / 个人限制 / 双限制")

        def update_mode(config: EntertainmentLimitConfig) -> EntertainmentLimitConfig:
            config.mode = mode
            return _ensure_mode_limit_defaults(config, mode)

        config = _update_group_entertainment_limit_config(ctx.group_id, update_mode)
        status = _format_entertainment_limit_status(_get_entertainment_limit_snapshot(ctx.group_id, ctx.user_id))
        return await ctx.asend_reply_msg(f"已将本群娱乐限制模式设置为{ENTERTAINMENT_MODE_SHOW_NAMES[config.mode]}\n{status}")

    # ================================ 显式字段设置 ================================ #
    # 这里保留“个人/群”显式配置入口，便于群管在不切换模式时预配参数。
    if len(parts) == 2 and (field_name := _parse_entertainment_field_name(parts[0])) is not None:
        value = _parse_positive_limit_value(parts[1])

        def update_field(config: EntertainmentLimitConfig) -> EntertainmentLimitConfig:
            setattr(config, field_name, value)
            return config

        config = _update_group_entertainment_limit_config(ctx.group_id, update_field)
        status = _format_entertainment_limit_status(_get_entertainment_limit_snapshot(ctx.group_id, ctx.user_id))
        field_show_name = "群聊限制" if field_name == "group_limit" else "个人限制"
        return await ctx.asend_reply_msg(f"已设置本群{field_show_name}为每日{value}次\n{status}")

    # ================================ 兼容数值设置 ================================ #
    # 不带后缀时按当前模式解释，保证老用法继续成立；双限制模式支持 `/pel A B`。
    try:
        numeric_values = [int(part) for part in parts]
    except ValueError:
        raise ReplyException(f"参数错误\n{_get_entertainment_limit_usage_text()}")

    assert_and_reply(len(numeric_values) in (1, 2), f"参数错误\n{_get_entertainment_limit_usage_text()}")

    if len(numeric_values) == 2:
        assert_and_reply(snapshot.config.mode == ENTERTAINMENT_MODE_HYBRID, "当前模式不是双限制，请先使用“/pel 模式 双限制”")
        user_limit, group_limit = numeric_values
        assert_and_reply(user_limit > 0 and group_limit > 0, "双限制模式下的两个数都必须大于 0")

        def update_hybrid(config: EntertainmentLimitConfig) -> EntertainmentLimitConfig:
            config.enabled = True
            config.user_limit = user_limit
            config.group_limit = group_limit
            return config

        config = _update_group_entertainment_limit_config(ctx.group_id, update_hybrid)
        status = _format_entertainment_limit_status(_get_entertainment_limit_snapshot(ctx.group_id, ctx.user_id))
        return await ctx.asend_reply_msg(f"已开启本群娱乐功能，并设置个人限制为每日{config.user_limit}次、群聊限制为每日{config.group_limit}次\n{status}")

    value = numeric_values[0]
    assert_and_reply(value > 0, "限制次数必须是大于 0 的整数；如需关闭请使用“/pel 0”")

    def update_by_mode(config: EntertainmentLimitConfig) -> EntertainmentLimitConfig:
        config.enabled = True
        if config.mode == ENTERTAINMENT_MODE_GROUP:
            config.group_limit = value
        elif config.mode == ENTERTAINMENT_MODE_USER:
            config.user_limit = value
        else:
            config.user_limit = value
            config = _ensure_mode_limit_defaults(config)
        return config

    config = _update_group_entertainment_limit_config(ctx.group_id, update_by_mode)
    status = _format_entertainment_limit_status(_get_entertainment_limit_snapshot(ctx.group_id, ctx.user_id))
    if config.mode == ENTERTAINMENT_MODE_GROUP:
        msg = f"已开启本群娱乐功能，并设置群聊限制为每日{config.group_limit}次"
    elif config.mode == ENTERTAINMENT_MODE_USER:
        msg = f"已开启本群娱乐功能，并设置个人限制为每日{config.user_limit}次"
    else:
        msg = f"已开启本群娱乐功能，并设置个人限制为每日{config.user_limit}次（群聊限制保持为每日{config.group_limit}次）"
    await ctx.asend_reply_msg(f"{msg}\n{status}")


pjsk_entertainment_limit_check = SekaiCmdHandler([
    "/pjsk entertainment count", "/pjsk_entertainment_count",
    "/pjsk娱乐功能次数", "/pec",
], regions=['jp'])
pjsk_entertainment_limit_check.check_cdrate(cd).check_wblist(gbl)
@pjsk_entertainment_limit_check.handle()
async def _(ctx: SekaiHandlerContext):
    _ensure_group_entertainment_context(ctx, "娱乐功能状态")
    snapshot = _get_entertainment_limit_snapshot(ctx.group_id, ctx.user_id)
    await ctx.asend_reply_msg(_format_entertainment_limit_status(snapshot))


# 猜曲封
pjsk_guess_cover = SekaiCmdHandler([
    "/pjsk guess cover", "/pjsk_guess_cover", 
    "/pjsk猜曲封", "/pjsk猜曲绘", "/猜曲绘", "/猜曲封",
], regions=['jp'])
pjsk_guess_cover.check_cdrate(cd).check_wblist(gbl)
@pjsk_guess_cover.handle()
async def _(ctx: SekaiHandlerContext):
    args = ctx.get_args().strip()
    diff, args = extract_diff(args, default='expert')
    assert_and_reply(diff in GUESS_COVER_DIFF_OPTIONS, f"可选难度：{', '.join(GUESS_COVER_DIFF_OPTIONS.keys())}")

    async def start_fn(gctx: GuessContext):
        music, cover_thumb, cover_img = await random_music(gctx.ctx, 'cover')
        logger.info(f"{gctx.scope_desc} 猜曲绘目标: {music['id']}")
        crop_img = await random_crop_image(cover_img, GUESS_COVER_DIFF_OPTIONS[diff])
        msg = await get_image_cq(crop_img)
        msg += f"{diff.upper()}模式猜曲绘{GUESS_COVER_DIFF_OPTIONS[diff].get_effect_tip_text()}"
        msg += f"，限时{int(GUESS_COVER_TIMEOUT.total_seconds())}秒"
        msg += "（无需回复，直接发送歌名/id/别名）"
        gctx.data['music'] = music
        gctx.data['cover_thumb'] = cover_thumb
        return msg

    await start_guess(
        ctx, '猜曲绘', GUESS_COVER_TIMEOUT, start_fn, 
        get_guess_music_check_fn('猜曲绘'), get_guess_music_stop_fn('猜曲绘'),
        send_guess_music_hint
    )


# 猜谱面
pjsk_guess_chart = SekaiCmdHandler([
    "/pjsk guess chart", "/pjsk_guess_chart", 
    "/pjsk猜谱面", "/猜谱面", "/pjsk猜铺面", "/猜铺面",
], regions=['jp'])
pjsk_guess_chart.check_cdrate(cd).check_wblist(gbl)
@pjsk_guess_chart.handle()
async def _(ctx: SekaiHandlerContext):
    args = ctx.get_args().strip()
    diff, args = extract_diff(args, default='expert')
    assert_and_reply(diff in GUESS_CHART_DIFF_OPTIONS, f"可选难度：{', '.join(GUESS_CHART_DIFF_OPTIONS.keys())}")

    async def start_fn(gctx: GuessContext):
        music, cover_thumb, _ = await random_music(gctx.ctx, 'cover')
        logger.info(f"{gctx.scope_desc} 猜谱面目标: {music['id']}")
        diff_info = await get_music_diff_info(gctx.ctx, music['id'])
        chart_diff = random.choice(['master', 'append']) if diff_info.has_append else 'master'
        chart_lv = diff_info.level[chart_diff]
        rate = random.uniform(
            GUESS_CHART_DIFF_OPTIONS[diff].rate_min, 
            GUESS_CHART_DIFF_OPTIONS[diff].rate_max
        )
        clip_chart = await generate_music_chart(
            gctx.ctx, music['id'], chart_diff, need_reply=False, 
            random_clip_length_rate=rate, style_sheet='guess',
            use_cache=False
        )
        msg = await get_image_cq(clip_chart)
        msg += f"{diff.upper()}模式猜谱面{GUESS_CHART_DIFF_OPTIONS[diff].get_effect_tip_text()}"
        msg += f"（谱面难度可能为MASTER或APPEND），限时{int(GUESS_CHART_TIMEOUT.total_seconds())}秒"
        msg += "（无需回复，直接发送歌名/id/别名）"
        gctx.data['music'] = music
        gctx.data['cover_thumb'] = cover_thumb
        gctx.data['chart_diff'] = chart_diff
        gctx.data['chart_lv'] = chart_lv
        return msg

    await start_guess(
        ctx, '猜谱面', GUESS_CHART_TIMEOUT, start_fn, 
        get_guess_music_check_fn('猜谱面'), get_guess_music_stop_fn('猜谱面'),
        send_guess_music_hint
    )


# 猜卡面
pjsk_guess_card = SekaiCmdHandler([
    "/pjsk guess card", "/pjsk_guess_card", 
    "/pjsk猜卡面", "/猜卡面", "/pjsk猜卡", "/猜卡",
], regions=['jp'])
pjsk_guess_card.check_cdrate(cd).check_wblist(gbl)
@pjsk_guess_card.handle()
async def _(ctx: SekaiHandlerContext):
    args = ctx.get_args().strip()
    diff, args = extract_diff(args, default='expert')
    assert_and_reply(diff in GUESS_CARD_DIFF_OPTIONS, f"可选难度：{', '.join(GUESS_CARD_DIFF_OPTIONS.keys())}")

    async def start_fn(gctx: GuessContext):
        card, card_img, after_training = await random_card(gctx.ctx)
        logger.info(f"{gctx.scope_desc} 猜卡面目标: {card['id']}")
        crop_img = await random_crop_image(card_img, GUESS_CARD_DIFF_OPTIONS[diff])
        msg = await get_image_cq(crop_img)
        msg += f"{diff.upper()}模式猜卡面{GUESS_CARD_DIFF_OPTIONS[diff].get_effect_tip_text()}"
        msg += f"，限时{int(GUESS_CARD_TIMEOUT.total_seconds())}秒"
        msg += "（无需回复，直接发送角色简称例如ick,saki）"
        gctx.data['card'] = card
        gctx.data['card_img'] = card_img
        gctx.data['after_training'] = after_training
        gctx.data['guessed'] = set()
        return msg

    async def check_fn(gctx: GuessContext):
        card, card_img, after_training = gctx.data['card'], gctx.data['card_img'], gctx.data['after_training']
        cid = get_cid_by_nickname(gctx.text)
        if cid is not None:
            gctx.data['guessed'].add(cid)
        if cid == card["characterId"]:
            await gctx.asend_reply_msg(f"你猜对了！\n{await get_card_title(gctx.ctx, card, after_training)}")
            await gctx.asend_msg(await get_image_cq(card_img, low_quality=True))
            gctx.guess_success = True
        if len(gctx.data['guessed']) > GUESS_CARD_CID_LIMIT:
            await gctx.asend_msg(f"猜卡面失败，正确答案：\n{await get_card_title(ctx, card, after_training)}")
            await gctx.asend_msg(await get_image_cq(card_img, low_quality=True))
            gctx.guess_success = True
    
    async def stop_fn(gctx: GuessContext):
        card, card_img, after_training = gctx.data['card'], gctx.data['card_img'], gctx.data['after_training']
        await gctx.asend_msg(f"猜卡面结束，正确答案：\n{await get_card_title(ctx, card, after_training)}")
        await gctx.asend_msg(await get_image_cq(card_img, low_quality=True))

    await start_guess(ctx, '猜卡面', GUESS_CARD_TIMEOUT, start_fn, check_fn, stop_fn, send_guess_card_hint)


# 听歌识曲
pjsk_guess_music = SekaiCmdHandler([
    "/pjsk guess music", "/pjsk_guess_music", 
    "/听歌识曲", "/pjsk听歌识曲", "/猜歌", "/pjsk猜歌", "/猜曲", "/pjsk猜曲",
], regions=['jp'])
pjsk_guess_music.check_cdrate(cd).check_wblist(gbl)
@pjsk_guess_music.handle()
async def _(ctx: SekaiHandlerContext):
    with TempFilePath('mp3', remove_after=timedelta(minutes=3)) as clipped_audio_path:
        args = ctx.get_args().strip()
        diff, args = extract_diff(args, default='expert')
        assert_and_reply(diff in GUESS_MUSIC_DIFF_OPTIONS, f"可选难度：{', '.join(GUESS_MUSIC_DIFF_OPTIONS.keys())}")

        async def start_fn(gctx: GuessContext):
            music, cover_thumb, audio_path = await random_music(gctx.ctx, 'audio')
            logger.info(f"{gctx.scope_desc} 听歌识曲目标: {music['id']}")
            await random_clip_audio(
                audio_path, clipped_audio_path, 
                length=GUESS_MUSIC_DIFF_OPTIONS[diff][0], 
                reverse=GUESS_MUSIC_DIFF_OPTIONS[diff][1],
            )
            tip_text = "（音频已反转）" if GUESS_MUSIC_DIFF_OPTIONS[diff][1] else ""
            msg = f"{diff.upper()}模式听歌识曲{tip_text}"
            msg += f"，限时{int(GUESS_MUSIC_TIMEOUT.total_seconds())}秒"
            msg += "（无需回复，直接发送歌名/id/别名）"
            gctx.data['music'] = music
            gctx.data['cover_thumb'] = cover_thumb
            return [msg, f"[CQ:record,file=file://{os.path.abspath(clipped_audio_path)}]"]

        await start_guess(
            ctx, '听歌识曲', GUESS_MUSIC_TIMEOUT, start_fn, 
            get_guess_music_check_fn('听歌识曲'), get_guess_music_stop_fn('听歌识曲'),
            send_guess_music_hint
        )


# 模拟抽卡
pjsk_spin_gacha = SekaiCmdHandler([
    "/单抽", "/十连", *[f"/{x}连" for x in (10, 50, 100, 150, 200)],
])
pjsk_spin_gacha.check_cdrate(cd).check_wblist(gbl)
@pjsk_spin_gacha.handle()
async def _(ctx: SekaiHandlerContext):
    args = ctx.get_args().strip()
    if not args:
        args = "-1"
    gacha = await parse_search_gacha_args(ctx, args)
    assert_and_reply(gacha, f"参数错误，{SINGLE_GACHA_HELP}")

    if "单抽" in ctx.trigger_cmd:
        count = 1
    elif "十连" in ctx.trigger_cmd:
        count = 10
    else:
        count = int(ctx.trigger_cmd.split("连")[0][1:])

    is_group_scope = is_group_msg(ctx.event)
    if is_group_scope:
        _check_group_entertainment_quota_available(ctx, _format_gacha_entertainment_quota_block_message)

    cards = await spin_gacha(ctx, gacha, count)
    ret = await ctx.asend_reply_msg(await get_image_cq(
        await compose_gacha_spin_image(ctx, gacha, cards), 
        low_quality=True
    ))
    if is_group_scope:
        _commit_group_entertainment_quota_usage(ctx)
    return ret
    
