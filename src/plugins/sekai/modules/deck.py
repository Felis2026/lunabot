from ...utils import *
from ..common import *
from ..handler import *
from ..asset import *
from ..draw import *
from .event import (
    BanEventQueryNoResult,
    get_event_banner_img,
    get_current_event,
    get_event_by_ban_name,
    parse_ban_event_query,
)
from .sk import get_wl_events
from .profile import (
    format_suite_missing_fields_error,
    get_player_bind_id,
    get_basic_profile,
    get_detailed_profile, 
    get_detailed_profile_card, 
    get_detailed_profile_card_filter,
    get_card_full_thumbnail,
)
from .education import get_user_challenge_live_info
from .card import get_unit_by_card_id, has_after_training
from .music import (
    search_music,
    MusicSearchOptions, 
    extract_diff, 
    is_valid_music, 
    get_music_cover_thumb,
    get_valid_musics,
    get_music_diff_info,
    musicmetas_json,
    LEADERBOARD_LIVETYPE_PLAY_INTERVAL,
)
from .mysekai import MYSEKAI_REGIONS
from src.services.deck_recommender.masterdata_spec import (
    get_deck_masterdata_specs,
    get_native_package_build_id,
    get_preview_expected_filenames,
    validate_resolved_paths,
)
from .deck_preview.assumed_cards import (
    AssumedCardsContext,
    PreviewCardError,
    inject_explicit_assumed_cards,
    select_result_card_masterdata,
    validate_preview_result_cards,
)
from .deck_preview.ruleset import (
    validate_event_card_closure,
    validate_world_bloom_support_tables,
)
from .deck_preview.protocol import PreviewEventResolution, PreviewStatus
from .deck_preview.router import (
    build_preview_guide,
    choose_world_bloom_chapter,
    extract_explicit_event_id,
    extract_preview_keyword,
    format_preview_result_title,
    select_world_bloom_turn,
)
from .deck_preview.scheduler import preview_manager
from .wl_args import (
    extract_wl_chapter_selector,
    extract_wl_role_selector,
    extract_wl_turn_selector,
    get_wl_simulation_max_turn,
    normalize_wl_args,
    remove_matched_text,
)
from sekai_deck_recommend_cpp import (
    DeckRecommendOptions, 
    DeckRecommendCardConfig, 
    DeckRecommendSingleCardConfig,
    DeckRecommendResult,
    DeckRecommendSaOptions,
    RecommendDeck,
)
from hashlib import md5
import json
import time


PREVIEW_CUTOVER_RETRY_SECONDS = 300


BOOST_BONUS_DICT: Dict[int, int] = {
    0: 1,
    1: 5,
    2: 10,
    3: 15,
    4: 20,
    5: 25,
    6: 27,
    7: 29,
    8: 31,
    9: 33,
    10: 35,
}

RECOMMEND_TIMEOUT_CFG = config.item('deck.timeout.default')
NO_EVENT_RECOMMEND_TIMEOUT_CFG = config.item('deck.timeout.no_event')
SINGLE_ALG_RECOMMEND_TIMEOUT_CFG = config.item('deck.timeout.single_alg')
BONUS_RECOMMEND_TIMEOUT_CFG = config.item('deck.timeout.bonus_target')
RECOMMEND_ALGS_CFG = config.item('deck.default_algs')
RECOMMEND_ALG_NAMES = {
    'dfs': '暴力搜索',
    'sa': '模拟退火',
    'ga': '遗传算法',
}


OMAKASE_MUSIC_ID = 10000
OMAKASE_MUSIC_DIFFS = ["master", "expert", "hard"]


# ======================= 默认配置 ======================= #

POWER_TARGET_KEYWORDS = ('综合力', '综合', '总合力', '总和', 'power')
SKILL_TARGET_KEYWORDS = ('倍率', '实效', 'skill', '时效')

SKILL_MAX_KEYWORDS = ("满技能", "满技", "skillmax", "技能满级", "slv4")
MASTER_MAX_KEYWORDS = ("满突破", "满破", "rankmax", "mastermax", "5破", "五破")
EPISODE_READ_KEYWORDS = ("剧情已读", "满剧情", "前后篇已读", "前后篇", "已读")
CANVAS_KEYWORDS = ("满画布", "全画布", "画布", "满画板", "全画板", "画板")
DISABLE_KEYWORDS = ("禁用", "disable")
TEAMMATE_POWER_KEYWORDS = ("队友综合力", "队友总合力", "队友综合", "队友总和")
TEAMMATE_SCOREUP_KEYWORDS = ("队友实效", "队友技能", "队友时效")
KEEP_AFTERTRAINING_STATE_KEYWORDS = ("bfes不变", "bf不变")

UNIT_FILTER_KEYWORDS = {
    "light_sound": ["纯ln", "仅ln"],
    "idol": ["纯mmj", "仅mmj"],
    "street": ["纯vbs", "仅vbs"],
    "theme_park": ["纯ws", "仅ws"],
    "school_refusal": ["纯25h", "纯25时", "纯25", "仅25h", "仅25时", "仅25"],
    "piapro": ["纯vs", "纯v", "仅vs", "仅v"],
}
MAX_PROFILE_KEYWORDS = ('顶配', '满配',)
SUB_MAX_PROFILE_KEYWORDS = ('次顶配', '次满配', '中配',)
CURRENT_DECK_KEYWORDS = ('当前', '目前')

MUSIC_COMPARE_KEYWORDS = ('歌曲比较', '歌曲排行', '歌曲排名', '歌曲推荐',)
WAR_PREPARE_KEYWORDS = ('战备', '备战')
MUSIC_COMPARE_DEFAULT_MUSIC_NUM = 8
MUSIC_COMPARE_CANDIDATE_MUSIC_NUM = 40
MUSCI_COMPARE_MAX_MUSIC_NUM = 5

MAX_KEYWORDS = ('最高', '最大', '最优', '最强', '最佳')
MIN_KEYWORDS = ('最低', '最小', '最差', '最弱', '最烂')
AVG_KEYWORDS = ('平均', '均值', '期望')

SKILL_ORDER_KEYWORDS = ('技能顺序', '技能排列')
SKILL_REF_KEYWORDS = ('技能抽取', '技能吸取')

BOOST_KEYWORDS = ('boost', '火', '体力', '体',)
AREA_ITEM_KEYWORDS = ('区域道具', '道具', 'areaitem', )
FINAL_CHAPTER_BADGE_KEYWORDS = ('tk牌', '终章牌', '带牌')

DEFAULT_CARD_CONFIG_12 = DeckRecommendCardConfig()
DEFAULT_CARD_CONFIG_12.disable = False
DEFAULT_CARD_CONFIG_12.level_max = True
DEFAULT_CARD_CONFIG_12.episode_read = True
DEFAULT_CARD_CONFIG_12.master_max = True
DEFAULT_CARD_CONFIG_12.skill_max = True
DEFAULT_CARD_CONFIG_12.canvas = False

DEFAULT_CARD_CONFIG_34bd = DeckRecommendCardConfig()
DEFAULT_CARD_CONFIG_34bd.disable = False
DEFAULT_CARD_CONFIG_34bd.level_max = True
DEFAULT_CARD_CONFIG_34bd.episode_read = False
DEFAULT_CARD_CONFIG_34bd.master_max = False
DEFAULT_CARD_CONFIG_34bd.skill_max = False
DEFAULT_CARD_CONFIG_34bd.canvas = False

NOCHANGE_CARD_CONFIG = DeckRecommendCardConfig()
NOCHANGE_CARD_CONFIG.disable = False
NOCHANGE_CARD_CONFIG.level_max = False
NOCHANGE_CARD_CONFIG.episode_read = False
NOCHANGE_CARD_CONFIG.master_max = False
NOCHANGE_CARD_CONFIG.skill_max = False
NOCHANGE_CARD_CONFIG.canvas = False

DEFAULT_TEAMMATE_POWER = 250000
DEFAULT_TEAMMATE_SCOREUP = 200


# ================================ 备战参数与格式化 ================================ #
# 这一块直接吸收 moe 的备战功能思路，但只保留当前仓库里实际存在的参数范围：
# - 备战 / 战备
# - 目标PT / 当前PT
# - 周回数
# 不额外扩展新的独立模式。
def parse_war_prepare_number(s: str) -> int:
    s = s.strip().lower().replace(',', '').replace('_', '')
    if s.endswith('k'):
        return int(float(s[:-1]) * 1000)
    if s.endswith('w'):
        return int(float(s[:-1]) * 10000)
    if s.endswith('e'):
        return int(float(s[:-1]) * 100000000)
    if s.endswith('万'):
        return int(float(s[:-1]) * 10000)
    if s.endswith('百万'):
        return int(float(s[:-2]) * 1000000)
    if s.endswith('亿'):
        return int(float(s[:-1]) * 100000000)
    return int(float(s))


def extract_war_prepare_options(args: str) -> tuple[dict, str]:
    ret = {}

    for keyword in WAR_PREPARE_KEYWORDS:
        if keyword in args:
            ret['war_prepare'] = True
            args = args.replace(keyword, "", 1).strip()
            break

    round_match = re.search(r'(\d+(?:\.\d+)?)周回', args)
    if round_match:
        ret['rounds_per_hour'] = float(round_match.group(1))
        if ret['rounds_per_hour'] <= 0:
            raise ReplyException("周回数必须大于0")
        args = args.replace(round_match.group(0), "", 1).strip()

    pt_patterns = (
        ('current_pt', r'(?:现在|今|现)\s*([0-9][0-9,._]*(?:\.\d+)?(?:k|w|e|万|百万|亿)?)'),
        ('target_pt', r'目标\s*([0-9][0-9,._]*(?:\.\d+)?(?:k|w|e|万|百万|亿)?)'),
    )
    for key, pattern in pt_patterns:
        match = re.search(pattern, args)
        if not match:
            continue
        try:
            ret[key] = parse_war_prepare_number(match.group(1))
        except Exception:
            raise ReplyException(f"解析{'当前PT' if key == 'current_pt' else '目标PT'}失败: {match.group(0)}")
        args = args.replace(match.group(0), "", 1).strip()

    return ret, args.strip()


def format_large_number_by_4(num: int | float) -> str:
    s = str(int(num))
    parts = []
    while len(s) > 4:
        parts.append(s[-4:])
        s = s[:-4]
    parts.append(s)
    return ','.join(reversed(parts))


def format_hours_minutes(total_hours: float) -> str:
    total_minutes = max(0, math.ceil(total_hours * 60))
    hours = total_minutes // 60
    minutes = total_minutes % 60
    return f"{hours}小时{minutes}分钟"


def format_wan_number(num: int | float, digits: int = 4) -> str:
    value = float(num) / 10000.0
    text = f"{value:.{digits}f}".rstrip('0').rstrip('.')
    return f"{text}万"


def format_hhmm_short(total_hours: float) -> str:
    total_minutes = max(0, math.ceil(total_hours * 60))
    hours = total_minutes // 60
    minutes = total_minutes % 60
    return f"{hours}h{minutes:02d}min"


# ================================ 终章目标角色与牌子模拟 ================================ #

FINAL_CHAPTER_HONOR_UNIT_NAME_MAP = {
    "light_sound": "lightsound",
    "idol": "idol",
    "street": "street",
    "theme_park": "themepark",
    "school_refusal": "schoolrefusal",
    "piapro": "piapro",
}


async def get_final_chapter_target_cid(
    ctx: SekaiHandlerContext,
    options: DeckRecommendOptions,
    use_current_deck: bool,
) -> Optional[int]:
    """
    获取终章当前上下文下的目标角色。

    终章的特殊加成和支援队在 cpp 里都与“队长角色”绑定，
    因此 Python 侧需要尽量把用户的“想冲某个角色”转换成确定目标。
    """
    if options.event_id != 180:
        return None

    if options.world_bloom_character_id:
        return options.world_bloom_character_id

    if options.fixed_characters:
        return options.fixed_characters[0]

    if options.fixed_cards:
        first_fixed_card = await ctx.md.cards.find_by_id(options.fixed_cards[0])
        if first_fixed_card:
            return first_fixed_card['characterId']

    if use_current_deck:
        return None

    return None


async def apply_final_chapter_target_character(
    ctx: SekaiHandlerContext,
    options: DeckRecommendOptions,
    use_current_deck: bool,
) -> Optional[int]:
    """
    把终章目标角色固化为固定队长角色，以贴合 cpp 的终章规则。
    """
    target_cid = await get_final_chapter_target_cid(ctx, options, use_current_deck)
    if options.event_id != 180 or target_cid is None:
        return target_cid

    if options.fixed_characters:
        assert_and_reply(
            options.fixed_characters[0] == target_cid,
            "终章指定角色与固定队长角色冲突，请保持一致",
        )
        return target_cid

    if options.fixed_cards:
        leader_card = await ctx.md.cards.find_by_id(options.fixed_cards[0])
        assert_and_reply(leader_card, f"找不到固定卡牌 {options.fixed_cards[0]}")
        assert_and_reply(
            leader_card['characterId'] == target_cid,
            "终章指定角色与固定卡牌队长不一致，请把目标角色的卡放在固定卡牌列表第一位",
        )
        options.best_skill_as_leader = False
        return target_cid

    if use_current_deck:
        assert_and_reply(
            False,
            "终章指定角色时暂不支持同时使用“当前”，请改为固定卡牌并把目标角色卡放在第一位",
        )

    options.fixed_characters = [target_cid]
    options.best_skill_as_leader = False
    return target_cid


async def get_final_chapter_wl2_chapter_no(
    ctx: SekaiHandlerContext,
    cid: int,
) -> Optional[int]:
    """
    获取目标角色在第二轮 WL 中对应的章节号，用于模拟终章牌子。
    """
    unit = get_unit_by_chara_id(cid)
    if not unit:
        return None

    if unit == "piapro":
        event = await ctx.md.events.find_by_id(179)
        if not event:
            return None
        chapters = await get_wl_events(ctx, event['id'])
    else:
        unit_events = [
            event for event in await ctx.md.events.get()
            if event.get('eventType') == 'world_bloom' and event.get('unit') == unit
        ]
        unit_events.sort(key=lambda x: x['startAt'])
        if len(unit_events) < 2:
            return None
        chapters = await get_wl_events(ctx, unit_events[1]['id'])

    for chapter in chapters:
        if chapter.get('wl_cid') == cid:
            return chapter.get('id', 0) // 1000
    return None


async def apply_final_chapter_badge_simulation(
    ctx: SekaiHandlerContext,
    profile: dict,
    target_cid: Optional[int],
    enabled: bool,
) -> None:
    """
    通过临时注入一枚 WL2 高阶 honor，模拟终章牌子加成。
    """
    if not enabled or target_cid is None:
        return

    chapter_no = await get_final_chapter_wl2_chapter_no(ctx, target_cid)
    assert_and_reply(chapter_no is not None, "找不到该角色在 WL2 中对应的章节，无法模拟终章牌子")

    unit = get_unit_by_chara_id(target_cid)
    unit_name = FINAL_CHAPTER_HONOR_UNIT_NAME_MAP.get(unit)
    assert_and_reply(unit_name is not None, "无法识别该角色对应的终章牌子类型")

    honors = await ctx.md.honors.get()
    asset_name = f"honor_top_001000_event_wl_2nd_{unit_name}_cp{chapter_no}"
    honor = find_by_predicate(
        honors,
        lambda x: x.get('assetbundleName') == asset_name and x.get('honorRarity') in ('high', 'highest'),
    )
    if honor is None:
        honor = find_by_predicate(
            honors,
            lambda x: (
                isinstance(x.get('assetbundleName'), str)
                and x['assetbundleName'].endswith(f"event_wl_2nd_{unit_name}_cp{chapter_no}")
                and x.get('honorRarity') in ('high', 'highest')
            ),
        )
    assert_and_reply(honor is not None, "找不到可用于模拟的终章牌子 honor")

    user_honors = profile.setdefault('userHonors', [])
    if find_by(user_honors, 'honorId', honor['id']):
        return

    user_honors.append({
        "honorId": honor['id'],
        "level": 1,
    })


def add_payload_segment(payloads: list[bytes], data: bytes):
    payloads.append(len(data).to_bytes(4, 'big'))
    payloads.append(data)

def build_multiparts_payload(payloads: list[bytes]) -> bytes:
    payload = b''.join(payloads)
    return compress_zstd(payload)


# ======================= 参数获取 ======================= #

# 从args中提取live类型 直接修改options 返回剩余参数
def extract_live_type(args: str, options: DeckRecommendOptions) -> str:
    if "多人" in args or '协力' in args: 
        options.live_type = "multi"
        args = args.replace("多人", "").replace("协力", "").strip()
    elif "单人" in args: 
        options.live_type = "solo"
        args = args.replace("单人", "").strip()
    elif "自动" in args or "auto" in args: 
        options.live_type = "auto"
        args = args.replace("自动", "").replace("auto", "").strip()
    else:
        options.live_type = "multi"
    return args.strip()

# ================================ 活动组卡首参数箱活简称 ================================ #

def get_leading_ban_event_token(args: str) -> Optional[str]:
    """
    获取位于剩余参数首位的箱活简称。

    只允许首参数充当活动，避免连续歌曲简称的后者误认为第二个活动；
    """
    normalized_args = normalize_wl_args(args)
    if not normalized_args:
        return None
    first_token = normalized_args.split(maxsplit=1)[0]
    return first_token if parse_ban_event_query(first_token) is not None else None


# 从args获取组卡目标活动（如果是wl则会同时返回cid）返回 (活动, cid, 剩余参数)
async def extract_target_event(
    ctx: SekaiHandlerContext, 
    args: str,
    match_type: str,
    default_return_current: bool,
    raise_if_not_found: bool,
) -> Tuple[Optional[dict], Optional[int], str]:
    args = normalize_wl_args(args)

    def assert_and_reply_or_return(condition: bool, msg: str):
        if raise_if_not_found:
            assert_and_reply(condition, msg)
        else:
            if not condition:
                return (None, None, args)

    # match_type为 simple/full/all 分别对应匹配 123/event123或者活动123/两者皆可
    match_simple = match_type in ('simple', 'all')
    match_full = match_type in ('full', 'all')
    
    forced_event_id = None

    # 总是替换终章
    for keyword in ('终章', ):
        if keyword in args:
            forced_event_id = 180
            args = args.replace(keyword, " event180 " if match_full else " 180 ").strip()

    # 解析成功后需要移除的文本
    event_matched_texts: list[str] = []
    wl_matched_texts: list[str] = []

    # ================================ WL章节选择 ================================ #
    # wl1/wl2 已统一为“第几次WL活动”；角色章节仍必须兼容原生的裸昵称语法
    if turn_selector := extract_wl_turn_selector(args):
        raise ReplyException(
            f"`{turn_selector.matched_text}` 表示第{turn_selector.turn}次WL活动，"
            "不能和具体活动ID一起当作章节使用；请改用“章节N”或“角色昵称”"
        )

    nickname_pairs = list(get_character_nickname_data().nickname_ids)
    chapter_id, chapter_arg = extract_wl_chapter_selector(args)
    chapter_nickname, role_arg = extract_wl_role_selector(
        args,
        [nickname for nickname, _ in nickname_pairs],
        allow_bare=True,
    )
    assert_and_reply_or_return(
        not (chapter_id and chapter_nickname),
        "不能同时指定章节号和角色章节",
    )
    if chapter_arg:
        wl_matched_texts.append(chapter_arg)
    if role_arg:
        wl_matched_texts.append(role_arg)
    
    # 解析活动id
    event_id = None
    if match_simple:
        simple_match = re.search(r"\b(\d{1,3})\b", args)
        if simple_match:
            event_id = int(simple_match.group(1))
            event_matched_texts.append(simple_match.group(0))
    if match_full:
        full_match = re.search(r"活动(\d+)|event(\d+)", args)
        if full_match:
            event_id = int(full_match.group(1) or full_match.group(2))
            event_matched_texts.append(full_match.group(0))
    if event_id is None and forced_event_id is not None:
        event_id = forced_event_id

    # 没有明确活动 ID 时，才允许首参数箱活简称成为活动目标；后续同类文本
    # 会保留给歌曲解析，例如“mnr5 mnr4”表示第五箱使用歌曲 mnr4。
    if event_id is None:
        if ban_event_token := get_leading_ban_event_token(args):
            ban_event = await get_event_by_ban_name(ctx, ban_event_token)
            event_id = ban_event["id"]
            event_matched_texts.append(ban_event_token)

    if event_id == 0:
        event_id = None
        event_matched_texts = []

    # 获取活动
    if event_id is None:
        # 没有指定活动的情况：寻找当前活动，如果没有就下一个活动
        if not default_return_current:
            assert_and_reply_or_return(False, "请指定一个要查询的活动，例如\"event140\"或\"活动140\"")

        event = await get_current_event(ctx, "next")
        assert_and_reply_or_return(event, """
找不到正在进行或即将开始的活动，指定团队+颜色进行模拟活动组卡，或使用\"/组卡help\"查看如何组往期活动
""".strip())
    else:
        event = await ctx.md.events.find_by_id(event_id)

        # 填充模拟终章
        if event_id == 180 and not event:
            event = { 'id': 180 }
        else:
            assert_and_reply_or_return(event, f"""
    活动{ctx.region}-{event_id}不存在，可以指定团队+颜色进行模拟活动组卡
    """.strip())

    # 获取WL章节
    wl_cid = None
    wl_events = await get_wl_events(ctx, event['id']) if event_id != 180 else []
    if wl_events:
        if not chapter_id and not chapter_nickname:
            # 获取默认章节
            if len(wl_events) == 1:
                # 只有一个章节就直接用
                chapter = wl_events[0]
            elif datetime.now() > datetime.fromtimestamp(event['aggregateAt'] / 1000):
                # 活动已经结束，默认使用最后一个章节
                wl_events.sort(key=lambda x: x['startAt'], reverse=True)
                chapter = wl_events[0]
            elif datetime.now() < datetime.fromtimestamp(event['startAt'] / 1000):
                # 活动还没开始，默认使用第一个章节
                wl_events.sort(key=lambda x: x['startAt'])
                chapter = wl_events[0]
            else:
                # 否则寻找 开始时间 <= 当前 <= 结束时间 的最晚的章节
                ok_chapters = []
                for chapter in wl_events:
                    start_time = datetime.fromtimestamp(chapter['startAt'] / 1000)
                    end_time = datetime.fromtimestamp(chapter['aggregateAt'] / 1000 + 1)
                    if start_time <= datetime.now() <= end_time:
                        ok_chapters.append(chapter)
                assert_and_reply_or_return(
                    ok_chapters,
                    "请指定一个要查询的WL章节，例如“event140 章节1”或“event140 角色miku”",
                )
                ok_chapters.sort(key=lambda x: x['startAt'], reverse=True)
                chapter = ok_chapters[0]
        elif chapter_id:
            chapter = find_by(wl_events, "id", 1000 * chapter_id + event['id'])
            assert_and_reply_or_return(chapter, f"活动 {ctx.region}-{event['id']} 没有章节 {chapter_id}")
        else: 
            cid = get_cid_by_nickname(chapter_nickname)
            chapter = find_by(wl_events, "wl_cid", cid)
            assert_and_reply_or_return(
                chapter,
                f"活动 {ctx.region}-{event['id']} 没有角色{chapter_nickname}的章节",
            )

        wl_cid = chapter['wl_cid']

    else:
        if event_id == 180 and chapter_nickname:
            # 终章不把昵称解释成“WL章节”，而是作为想冲的目标角色继续向下传递。
            wl_cid = get_cid_by_nickname(chapter_nickname)
            assert_and_reply_or_return(wl_cid, f"无法识别终章目标角色 {chapter_nickname}")
        elif chapter_id:
            assert_and_reply_or_return(
                False,
                f"活动 {ctx.region}-{event['id']} 不是WL活动，无法指定章节",
            )
        elif chapter_nickname and re.match(
            r"(?i)^(?:角色|role)",
            role_arg or "",
        ):
            # 显式“角色xx”是章节选择器；用于非 WL 活动时应给出明确错误。
            assert_and_reply_or_return(
                False,
                f"活动 {ctx.region}-{event['id']} 不是WL活动，无法指定章节",
            )
        else:
            # 原版会把普通活动中的裸角色昵称留给后续参数解析，不能提前吃掉。
            wl_matched_texts = []

    # 确认匹配到活动
    for text in event_matched_texts:
        args = remove_matched_text(args, text)
    for text in wl_matched_texts:
        args = remove_matched_text(args, text)
    
    return event, wl_cid, args


# ================================ CN未来活动Preview解析 ================================ #

async def extract_preview_target_event(
    ctx: SekaiHandlerContext,
    args: str,
    options: DeckRecommendOptions,
    *,
    preview_requested: bool,
    match_type: str,
    command_kind: str,
) -> tuple[str, Optional[PreviewEventResolution], bool]:
    """
    在原生活动解析前处理 CN clean miss。

    返回 `(剩余参数, resolution, 是否已设置活动)`；玩家 Context 始终保持 CN，
    只有规则活动、绘图素材来源和后端池通过 resolution 单独传递。
    """

    if ctx.region != "cn":
        return args, None, False

    args = normalize_wl_args(args)
    event_id, event_matched_text = extract_explicit_event_id(
        args,
        match_type=match_type,
    )
    turn_selector = extract_wl_turn_selector(args)
    jp_ctx = SekaiHandlerContext.from_region("jp")
    selected_role_nickname: str | None = None
    selected_role_cid: int | None = None
    rule_event: dict | None = None
    resolved_cn_event: dict | None = None
    leading_ban_event_token: str | None = None

    # ================================ 首参数箱活简称定位 ================================ #
    # CN 有对应箱活时仍走原生组卡规则；仅 CN 缺失且 JP 存在时，才把解析出的
    # JP 活动 ID 交给既有预览引导/预览路由。
    if event_id is None:
        leading_ban_event_token = get_leading_ban_event_token(args)
        if leading_ban_event_token:
            try:
                resolved_cn_event = await get_event_by_ban_name(
                    ctx,
                    leading_ban_event_token,
                )
                event_id = resolved_cn_event["id"]
            except BanEventQueryNoResult:
                rule_event = await get_event_by_ban_name(
                    jp_ctx,
                    leading_ban_event_token,
                )
                event_id = rule_event["id"]
            event_matched_text = leading_ban_event_token

    # ================================ 第N次WL动态定位 ================================ #
    if turn_selector and preview_requested:
        assert_and_reply(
            not turn_selector.force_simulated,
            "“预览”和“模拟WL”不能同时使用；预览只读取 JP 已存在的真实活动规则",
        )
        assert_and_reply(
            event_id is None,
            "不能同时指定活动ID和第几次WL；具体活动请改用“章节N”或“角色昵称”",
        )
        nickname_pairs = list(get_character_nickname_data().nickname_ids)
        selected_role_nickname, role_arg = extract_wl_role_selector(
            args,
            [nickname for nickname, _ in nickname_pairs],
            allow_bare=True,
        )
        assert_and_reply(
            selected_role_nickname,
            f"第{turn_selector.turn}次WL预览还需要指定目标角色",
        )
        selected_role_cid = get_cid_by_nickname(selected_role_nickname)
        assert_and_reply(
            selected_role_cid is not None,
            f"无法识别WL目标角色 {selected_role_nickname}",
        )
        try:
            rule_event = select_world_bloom_turn(
                await jp_ctx.md.events.get(),
                await jp_ctx.md.world_blooms.get(),
                character_id=selected_role_cid,
                turn=turn_selector.turn,
            )
        except ValueError as exc:
            raise ReplyException(str(exc)) from exc
        event_id = rule_event["id"]
        args = remove_matched_text(args, turn_selector.matched_text)
        args = remove_matched_text(args, role_arg)
        event_matched_text = None
    elif preview_requested and event_id is None:
        raise ReplyException(
            "未来活动预览必须指定明确活动ID，或使用“第N次WL 角色名 预览”"
        )
    elif event_id is None:
        return args, None, False

    assert event_id is not None
    cn_event = resolved_cn_event or await ctx.md.events.find_by_id(event_id)
    native_args = args
    if cn_event and leading_ban_event_token:
        native_args = normalize_wl_args(
            f"event{event_id} "
            f"{remove_matched_text(args, leading_ban_event_token)}"
        )
    if not preview_manager.enabled():
        # 关闭开关必须完整恢复原生组卡路径；粘性检测记录保留在磁盘，重新
        # 启用后继续生效，但不能在关闭期间改变命令行为。
        if preview_requested:
            raise ReplyException("国服未来活动预览功能未启用")
        return native_args, None, False

    state = preview_manager.get_state()
    known_preview_event = (
        event_id in preview_manager.tracked_event_ids()
    )

    # ================================ CN正式数据优先 ================================ #
    if cn_event:
        if known_preview_event and not preview_manager.is_official_active(event_id):
            preview_manager.mark_cn_detected(
                event_id,
                str(cn_event.get("eventType", "")),
            )
            raise ReplyException("国服活动数据正在同步，请稍后再试")
        if selected_role_nickname:
            # 第N次WL已经唯一定位到真实活动；改写成明确活动+角色后交回原生解析。
            args = normalize_wl_args(
                f"event{event_id} 角色{selected_role_nickname} {args}"
            )
        elif leading_ban_event_token:
            args = native_args
        return args, None, False
    if (
        preview_manager.is_official_active(event_id)
        or preview_manager.is_cn_detected(event_id)
    ):
        # CN_DETECTED/OFFICIAL_ACTIVE 后即使 CN 缓存短暂回退，也绝不重新使用 JP 规则。
        raise ReplyException("国服活动数据正在同步，请稍后再试")

    rule_event = rule_event or await jp_ctx.md.events.find_by_id(event_id)
    if not rule_event:
        if preview_requested:
            raise ReplyException(f"日服也不存在活动 #{event_id}，无法预览")
        return args, None, False

    event_type = rule_event.get("eventType")
    if event_type not in {"marathon", "world_bloom"}:
        if preview_requested:
            raise ReplyException(
                f"活动 #{event_id} 暂不支持预览；"
                "目前仅支持普通活动和 World Link 活动"
            )
        return args, None, False
    if not preview_manager.event_allowed_by_config(event_id, event_type):
        if preview_requested:
            raise ReplyException(f"活动 #{event_id} 暂未开放预览")
        return args, None, False

    if not preview_requested:
        explicit_cn = str(ctx.original_trigger_cmd).lower().startswith("/cn")
        raise ReplyException(build_preview_guide(
            event_id,
            explicit_cn=explicit_cn,
            command_kind=command_kind,
        ))

    if state.status != PreviewStatus.READY:
        preview_manager.wake()
        if state.status == PreviewStatus.ERROR:
            raise ReplyException("预览组卡暂时不可用，请稍后再试")
        raise ReplyException("预览数据正在准备，请稍后再试")
    if event_id not in set(state.manifest.get("event_ids", [])):
        raise ReplyException(f"活动 #{event_id} 暂时无法预览，请稍后再试")

    # ================================ Preview WL章节选择 ================================ #
    wl_cid = None
    chapter_arg = None
    role_arg = None
    if event_type == "world_bloom":
        chapters = [
            chapter
            for chapter in await jp_ctx.md.world_blooms.get()
            if chapter.get("eventId") == event_id
        ]
        chapter_no, chapter_arg = extract_wl_chapter_selector(args)
        if selected_role_cid is None:
            nickname_pairs = list(get_character_nickname_data().nickname_ids)
            role_nickname, role_arg = extract_wl_role_selector(
                args,
                [nickname for nickname, _ in nickname_pairs],
                # Preview 只替换活动规则来源，命令语法必须与原生 WL 组卡一致。
                allow_bare=True,
            )
            selected_role_cid = (
                get_cid_by_nickname(role_nickname)
                if role_nickname
                else None
            )
        assert_and_reply(
            not (chapter_no and selected_role_cid),
            "不能同时指定 WL 章节号和角色章节",
        )
        try:
            chapter = choose_world_bloom_chapter(
                chapters,
                chapter_no=chapter_no,
                character_id=selected_role_cid,
            )
        except ValueError as exc:
            raise ReplyException(str(exc)) from exc
        wl_cid = chapter["gameCharacterId"]
    else:
        chapter_no, chapter_arg = extract_wl_chapter_selector(args)
        role_nickname, role_arg = extract_wl_role_selector(
            args,
            [nickname for nickname, _ in get_character_nickname_data().nickname_ids],
            allow_bare=False,
        )
        assert_and_reply(
            chapter_no is None and role_nickname is None,
            f"活动 #{event_id} 不是 WL，不能指定章节",
        )

    for matched_text in (event_matched_text, chapter_arg, role_arg):
        if matched_text:
            args = remove_matched_text(args, matched_text)

    options.event_id = event_id
    options.world_bloom_character_id = wl_cid
    resolution = PreviewEventResolution(
        event_id=event_id,
        event_type=event_type,
        rule_event=rule_event,
        world_bloom_character_id=wl_cid,
        scope_fingerprint=state.scope_fingerprint,
        future_card_ids=frozenset(preview_manager.future_card_ids()),
    )
    return args, resolution, True


# 从args同时获取组卡目标活动或者指定属性&团模拟活动 直接修改options 返回剩余参数
async def extract_target_event_or_simulate_event(
    ctx: SekaiHandlerContext, 
    args: str,
    options: DeckRecommendOptions,
) -> str:
    args = normalize_wl_args(args)

    # ================================ 终章目标角色直通 ================================ #
    # 终章和普通 WL 不同，角色名表示“想冲的目标角色”，不是章节昵称。
    # 这里直接在上层消化掉这层语义，避免落到普通 WL 章节解析里。
    final_keyword_text = None
    for keyword in ("终章", "活动180", "event180"):
        if keyword in args:
            final_keyword_text = keyword
            break
    if final_keyword_text:
        for nickname, cid in get_character_nickname_data().nickname_ids:
            if nickname in args:
                args = args.replace(final_keyword_text, "", 1).replace(nickname, "", 1).strip()
                options.event_id = 180
                options.world_bloom_character_id = cid
                return args

    # ================================ WL活动轮次 ================================ #
    # 真实轮次完全按 worldBlooms 中是否包含目标角色动态定位，VS 也不再依赖
    # [140, 179] 这类会随第三轮实装失效的固定活动 ID。
    if turn_selector := extract_wl_turn_selector(args):
        args_without_turn = remove_matched_text(args, turn_selector.matched_text)
        explicit_event_match = (
            re.search(r"(?:活动|event)\s*(\d+)", args_without_turn, re.IGNORECASE)
            or re.search(r"(?:^|\s)(\d{1,3})(?=$|\s)", args_without_turn)
        )
        assert_and_reply(
            not explicit_event_match,
            "不能同时指定活动ID和第几次WL；具体活动的章节请使用“章节N”或“角色昵称”",
        )

        nickname_pairs = list(get_character_nickname_data().nickname_ids)
        wl_nickname, nickname_arg = extract_wl_role_selector(
            args_without_turn,
            [nickname for nickname, _ in nickname_pairs],
            allow_bare=True,
        )
        assert_and_reply(
            wl_nickname,
            f"第{turn_selector.turn}次WL还需要指定目标角色，例如“第{turn_selector.turn}次WL ena”",
        )
        cid = get_cid_by_nickname(wl_nickname)
        assert_and_reply(cid, f"无法识别WL目标角色 {wl_nickname}")
        remaining_args = remove_matched_text(args_without_turn, nickname_arg)
        unit = get_unit_by_chara_id(cid)

        if not turn_selector.force_simulated:
            try:
                target_event = select_world_bloom_turn(
                    await ctx.md.events.get(),
                    await ctx.md.world_blooms.get(),
                    character_id=cid,
                    turn=turn_selector.turn,
                )
                options.event_id = target_event["id"]
                options.world_bloom_character_id = cid
                return remaining_args
            except ValueError:
                raise ReplyException(
                    f"找不到{get_region_name(ctx.region)}已实装的第"
                    f"{turn_selector.turn}次WL（角色{wl_nickname}）；"
                    f"如需强制模拟请使用“模拟WL{turn_selector.turn} {wl_nickname}”"
                )

        # 只有“模拟WLN”显式语法才允许进入没有真实活动规则的模拟分支；
        # 最大轮次由 start.sh 完成原生能力验证后注入，不只凭用户参数放行。
        simulation_max_turn = get_wl_simulation_max_turn()
        assert_and_reply(
            1 <= turn_selector.turn <= simulation_max_turn,
            f"当前原生组卡库仅支持模拟 WL1～WL{simulation_max_turn}；"
            f"第{turn_selector.turn}次 WL 如已实装，请使用"
            "“第N次WL 角色名”动态定位",
        )
        options.event_unit = unit
        options.world_bloom_event_turn = turn_selector.turn
        options.world_bloom_character_id = cid
        return remaining_args

    # 25需要优先匹配团队，对于活动id内包含25的情况，必须让团队的25两边不能有数字或者"event"、"活动"
    # 这样只有想以单独数字25指定25期活动时可能产生歧义，为该情况额外添加用户提示
    # 匹配团名和属性名，并检查25的情况
    attr, args = extract_card_attr(args, default=None)
    unit, new_args = extract_unit(args, default=None)
    index_25 = args.find('25')
    if unit == "piapro" and 'vs' not in args:
        # vs加成活动只能匹配vs，避免v的误匹配
        unit = None
    elif unit == "school_refusal" and index_25 != -1:
        left = args[index_25 - 1] if index_25 - 1 >= 0 else ' '
        right = args[index_25 + 2] if index_25 + 2 < len(args) else ' '
        if left.isdigit() or right.isdigit() or left in ('t', '活'):
            # 取消匹配，把参数让给活动匹配
            unit = None
        else:
            args = new_args
    else:
        args = new_args

    if unit and attr:
        options.event_unit = unit
        options.event_attr = attr
        return args
    if unit or attr:
        hint = f"你在参数中指定了{'团名' if unit else '颜色'}，"
        hint += f"这意味着你需要加成是指定团+颜色的模拟活动组卡，"
        hint += f"请再指定{'颜色' if unit else '团名'}\n"
        hint += f"如果想限制仅某团/某颜色卡牌上场，请使用"
        if unit: hint += f" \"仅{UNIT_ABBRS[unit]}\""
        if attr: hint += f" \"仅{CARD_ATTR_ABBR[attr]}\""
        hint += "\n"
        if index_25 != -1:
            hint += "如果你想指定是25期活动而不是25时，请使用 \"event25\""
        raise ReplyException(hint.strip())

    # 匹配活动
    event, wl_cid, args = await extract_target_event(
        ctx, 
        args, 
        match_type="all", 
        default_return_current=True, 
        raise_if_not_found=True,
    )
    options.event_id = event['id']
    options.world_bloom_character_id = wl_cid
    return args

# 从args中提取组卡目标
def extract_target(args: str, options: DeckRecommendOptions) -> str:
    options.target = "score"

    for keyword in POWER_TARGET_KEYWORDS:
        if keyword in args:
            args = args.replace(keyword, "").strip()
            options.target = "power"
            break

    for keyword in SKILL_TARGET_KEYWORDS:
        if keyword in args:
            args = args.replace(keyword, "").strip()
            options.target = "skill"
            break
    
    return args.strip()

# 从args中提取随机因素选择策略
def extract_random_strategy(
    args: str, 
    options: DeckRecommendOptions,
    default_skill_order_strategy: str,
    default_skill_reference_strategy: str,
) -> str:
    for seg in args.split():
        # 技能顺序选择
        for keyword in SKILL_ORDER_KEYWORDS:
            if keyword in seg:
                if any(kw in seg for kw in MAX_KEYWORDS):
                    options.skill_order_choose_strategy = "max"
                elif any(kw in seg for kw in MIN_KEYWORDS):
                    options.skill_order_choose_strategy = "min"
                elif any(kw in seg for kw in AVG_KEYWORDS):
                    options.skill_order_choose_strategy = "average"
                else:
                    # 指定顺序
                    options.skill_order_choose_strategy = "specific"
                    try:
                        order = seg.replace(keyword, "").strip()
                        order = [int(c) - 1 for c in order]
                        assert set(order) == set(range(5))
                        options.specific_skill_order = order
                    except:
                        raise ReplyException("""
指定技能顺序方式:
最优顺序: /指令 ... 技能顺序最优
最差顺序: /指令 ... 技能顺序最差
平均顺序: /指令 ... 技能顺序平均
特定顺序: /指令 ... 技能顺序12345
""".strip())
                args = args.replace(seg, "", 1).strip()
        # 技能吸取选择
        for keyword in SKILL_REF_KEYWORDS:
            if keyword in seg:
                if any(kw in seg for kw in MAX_KEYWORDS):
                    options.skill_reference_choose_strategy = "max"
                elif any(kw in seg for kw in MIN_KEYWORDS):
                    options.skill_reference_choose_strategy = "min"
                elif any(kw in seg for kw in AVG_KEYWORDS):
                    options.skill_reference_choose_strategy = "average"
                args = args.replace(seg, "", 1).strip()
    
    if options.skill_order_choose_strategy is None:
        options.skill_order_choose_strategy = default_skill_order_strategy
    if options.skill_reference_choose_strategy is None:
        options.skill_reference_choose_strategy = default_skill_reference_strategy

    return args.strip()
                
# 从args中提取固定卡牌
def extract_fixed_cards_and_characters(args: str, options: DeckRecommendOptions) -> str:
    args = args.replace('＃', '#')
    if '#' in args:
        args, fixed_args = args.split('#', 1)
        fixed_cards, fixed_characters = [], []
        try:
            # 固定卡牌
            fixed_cards = list(map(int, fixed_args.strip().split()))
        except:
            try:
                # 固定角色
                for seg in fixed_args.strip().split():
                    nickname, _ = extract_nickname_from_args(seg)
                    assert nickname
                    fixed_characters.append(get_cid_by_nickname(nickname))
                assert fixed_characters
            except:
                raise ReplyException("""
格式错误，#固定卡牌 或 #固定角色 必须放在最后，示例:
/组卡指令 其他参数 #123 456 789...
/组卡指令 其他参数 #miku rin...
如果你想在固定卡牌时同时指定该卡牌的状态，示例:
/组卡指令 123满技能满破 #123
""".strip())

        if fixed_cards:
            assert_and_reply(len(fixed_cards) <= 5, f"固定卡牌数量不能超过5张")
            assert_and_reply(len(set(fixed_cards)) == len(fixed_cards), "固定卡牌不能重复")
            options.fixed_cards = fixed_cards

        elif fixed_characters:
            assert_and_reply(len(fixed_characters) <= 5, f"固定角色数量不能超过5个")
            assert_and_reply(len(set(fixed_characters)) == len(fixed_characters), "固定角色不能重复")
            options.fixed_characters = fixed_characters

    return args.strip()

# 从args中提取卡牌设置
def extract_card_config(args: str, options: DeckRecommendOptions, default_nochange=False) -> str:
    def get_prefix_digit(s: str) -> Optional[int]:
        d = ""
        for c in s:
            if c.isdigit():
                d += c
            else:
                break
        return int(d) if d else None

    def has_config_keyword(s: str):
        return any(
            keyword in s for keyword 
            in DISABLE_KEYWORDS + SKILL_MAX_KEYWORDS + MASTER_MAX_KEYWORDS + EPISODE_READ_KEYWORDS + CANVAS_KEYWORDS
        )

    def apply_card_config(args: str, cfgs: List[DeckRecommendCardConfig]) -> str:
        for keyword in DISABLE_KEYWORDS:
            if keyword in args:
                for cfg in cfgs:
                    cfg.disable = True
                args = args.replace(keyword, "").strip()
                break
        for keyword in SKILL_MAX_KEYWORDS:
            if keyword in args:
                for cfg in cfgs:
                    cfg.skill_max = True
                args = args.replace(keyword, "").strip()
                break
        for keyword in MASTER_MAX_KEYWORDS:
            if keyword in args:
                for cfg in cfgs:
                    cfg.master_max = True
                args = args.replace(keyword, "").strip()
                break
        for keyword in EPISODE_READ_KEYWORDS:
            if keyword in args:
                for cfg in cfgs:
                    cfg.episode_read = True
                args = args.replace(keyword, "").strip()
                break
        for keyword in CANVAS_KEYWORDS:
            if keyword in args:
                for cfg in cfgs:
                    cfg.canvas = True
                args = args.replace(keyword, "").strip()
                break
        return args.strip()

    if default_nochange:
        options.rarity_1_config = NOCHANGE_CARD_CONFIG
        options.rarity_2_config = NOCHANGE_CARD_CONFIG
        options.rarity_3_config = NOCHANGE_CARD_CONFIG
        options.rarity_4_config = NOCHANGE_CARD_CONFIG
        options.rarity_birthday_config = NOCHANGE_CARD_CONFIG
    else:
        options.rarity_1_config = DEFAULT_CARD_CONFIG_12
        options.rarity_2_config = DEFAULT_CARD_CONFIG_12
        options.rarity_3_config = DEFAULT_CARD_CONFIG_34bd
        options.rarity_4_config = DEFAULT_CARD_CONFIG_34bd
        options.rarity_birthday_config = DEFAULT_CARD_CONFIG_34bd

    segs = args.split()

    # 稀有度单独设置
    for rarity, cfg in [
        ('一星', options.rarity_1_config),
        ('二星', options.rarity_2_config),
        ('三星', options.rarity_3_config),
        ('四星', options.rarity_4_config),
        ('生日', options.rarity_birthday_config),
    ]:
        for seg in segs:
            if seg.startswith(rarity) and has_config_keyword(seg):
                apply_card_config(seg, [cfg])
                args = args.replace(seg, "").strip()
    
    # 卡牌单独设置
    single_card_configs = []
    for seg in segs:
        card_id = get_prefix_digit(seg)
        if card_id is not None and has_config_keyword(seg):
            cfg = DeckRecommendSingleCardConfig()
            cfg.card_id = card_id
            cfg.level_max = True
            apply_card_config(seg, [cfg])
            single_card_configs.append(cfg)
            args = args.replace(seg, "").strip()
    options.single_card_configs = single_card_configs

    # 全体设置
    args = apply_card_config(args, [
        options.rarity_1_config,
        options.rarity_2_config,
        options.rarity_3_config,
        options.rarity_4_config,
        options.rarity_birthday_config,
    ])

    # bfes不变设置
    options.keep_after_training_state = False
    for keyword in KEEP_AFTERTRAINING_STATE_KEYWORDS:
        if keyword in args:
            options.keep_after_training_state = True
            args = args.replace(keyword, "").strip()
            break

    return args

# 从args中提取多人live相关设置
def extract_multilive_options(args: str, options: DeckRecommendOptions) -> str:
    if options.live_type != "multi":
        return args.strip()

    options.multi_live_teammate_power = DEFAULT_TEAMMATE_POWER
    options.multi_live_teammate_score_up = DEFAULT_TEAMMATE_SCOREUP

    segs = args.split()
    for seg in segs:
        for keyword in TEAMMATE_POWER_KEYWORDS:
            if keyword in seg:
                value = seg.replace(keyword, "").strip()
                try:
                    options.multi_live_teammate_power = parse_large_number(value)
                    args = args.replace(seg, "", 1).strip()
                    break
                except:
                    raise ReplyException(f"无法解析指定的队友综合力\"{value}\"")
        for keyword in TEAMMATE_SCOREUP_KEYWORDS:
            if keyword in seg:
                value = seg.replace(keyword, "").strip()
                try:
                    options.multi_live_teammate_score_up = int(value)
                    args = args.replace(seg, "", 1).strip()
                    break
                except:
                    raise ReplyException(f"无法解析指定的队友实效\"{value}\"")
        for keyword in SKILL_TARGET_KEYWORDS:
            if keyword in seg:
                value = seg.replace(keyword, "").strip()
                if value.isdigit():
                    options.multi_live_score_up_lower_bound = int(value)
                    options.multi_live_teammate_score_up = int(value)
                    args = args.replace(seg, "", 1).strip()
                    break

    return args.strip()

# 从args中提取歌曲和难度，返回用于匹配歌曲的参数
async def extract_music_and_diff(
    ctx: SekaiHandlerContext, 
    args: str, 
    options: DeckRecommendOptions, 
    rec_type: str, 
    live_type: str, 
    additional_args: dict,
) -> str:
    jp_ctx = SekaiHandlerContext.from_region('jp')
    search_options = MusicSearchOptions(
        use_emb=False,
        use_id=True,
        use_nidx=True,
        raise_when_err=False,
    )

    # 歌曲比较模式匹配（多首歌曲）
    if additional_args.get('music_compare'):
        segs = args.split()
        assert_and_reply(len(segs) <= MUSCI_COMPARE_MAX_MUSIC_NUM, f"最多只能指定 {MUSCI_COMPARE_MAX_MUSIC_NUM} 首歌曲进行比较")
        additional_args['music_diffs_to_compare'] = []
        for seg in segs:
            music_diff, seg = extract_diff(seg, default='master')
            search_options.diff = music_diff
            music = (await search_music(jp_ctx, seg, search_options)).music
            assert_and_reply(music, f"在组卡支持的所有歌曲中找不到\"{seg}\"")
            additional_args['music_diffs_to_compare'].append((music['id'], music_diff, seg))
        return ""

    # 一般歌曲匹配（单首歌曲&默认判断）
    options.music_diff, args = extract_diff(args, default=None)
    args = args.strip()
    if args:
        search_options.diff = options.music_diff
        music = (await search_music(jp_ctx, args, search_options)).music
        err_msg = f"在组卡支持的所有歌曲中找不到\"{args}\""
        if len(args.split()) > 1:
            err_msg += "，如果你要对多首歌曲进行比较，请加上\"歌曲比较\""
        err_msg += f"，发送\"{ctx.trigger_cmd}help\"查看帮助"
        assert_and_reply(music, err_msg)
        options.music_id = music['id']

    # 已指定歌曲和难度
    if options.music_id is not None and options.music_diff is not None:
        return args

    # 只指定歌曲未指定难度：默认使用master
    if options.music_id is not None and options.music_diff is None:
        options.music_diff = 'master'
        return args
    
    # 未指定歌曲：查找默认歌曲
    default_musicdiffs = config.get('deck.default_musicdiffs')[rec_type]
    if isinstance(default_musicdiffs, dict):
        default_musicdiffs = default_musicdiffs[live_type]
    for mid, diff in default_musicdiffs:
        if mid == 'omakase': mid = OMAKASE_MUSIC_ID
        if mid == OMAKASE_MUSIC_ID or await is_valid_music(ctx, mid, leak=False, diff=diff):
            options.music_id = mid
            options.music_diff = diff
            additional_args['use_default_music'] = True
            return args
        
    raise Exception("组卡未正确配置默认歌曲")

# 从args中提取不在options中的参数
def extract_addtional_options(args: str) -> Tuple[dict, str]:
    ret = {}

    war_prepare, args = extract_war_prepare_options(args)
    ret.update(war_prepare)

    for keyword in FINAL_CHAPTER_BADGE_KEYWORDS:
        if keyword in args:
            ret['final_chapter_badge_sim'] = True
            args = args.replace(keyword, "", 1).strip()
            break

    for boost in reversed(BOOST_BONUS_DICT.keys()):
        for keyword in BOOST_KEYWORDS:
            kw = f"{boost}{keyword}"
            if kw in args:
                ret['boost'] = boost
                args = args.replace(kw, "", 1).strip()
                break

    for level in reversed(range(1, 21)):
        for keyword in AREA_ITEM_KEYWORDS:
            if keyword not in args:
                continue
            for kw in (
                f"{keyword}{level}级",
                f"{level}级{keyword}",
                f"{keyword}{level}",
                f"{level}{keyword}",
            ):
                if kw in args:
                    ret['area_item_level'] = level
                    args = args.replace(kw, "", 1).strip()
                    break

    for unit, keywords in UNIT_FILTER_KEYWORDS.items():
        for keyword in keywords:
            if keyword in args:
                ret['unit_filter'] = unit
                args = args.replace(keyword, "", 1).strip()
                break

    for names in CARD_ATTR_NAMES:
        for name in names:
            keyword = '纯' + name
            if keyword in args:
                ret['attr_filter'] = names[0]
                args = args.replace(keyword, "", 1).strip()
                break
            keyword = '仅' + name
            if keyword in args:
                ret['attr_filter'] = names[0]
                args = args.replace(keyword, "", 1).strip()
                break

    for keyword in SUB_MAX_PROFILE_KEYWORDS:
        if keyword in args:
            ret['sub_max_profile'] = True
            args = args.replace(keyword, "", 1).strip()
            break
    
    for keyword in MAX_PROFILE_KEYWORDS:
        if keyword in args:
            ret['max_profile'] = True
            args = args.replace(keyword, "", 1).strip()
            break

    for keyword in CURRENT_DECK_KEYWORDS:
        if keyword in args:
            ret['use_current_deck'] = True
            args = args.replace(keyword, "", 1).strip()
            break

    for keyword in MUSIC_COMPARE_KEYWORDS:
        if keyword in args:
            ret['music_compare'] = True
            args = args.replace(keyword, "", 1).strip()

    ret['excluded_cards'] = []
    segs = args.split()
    for seg in segs:
        if seg[0] == '-' and seg[1:].isdigit():
            try:
                x = int(seg[1:])
                if 0 < x < 5000:
                    ret['excluded_cards'].append(x)
                    args = args.replace(seg, "", 1).strip()
            except ValueError:
                pass

    return ret, args.strip()


# 从args中提取活动组卡参数
async def extract_event_options(ctx: SekaiHandlerContext, args: str) -> Dict:
    args = ctx.get_args().strip().lower()
    options = DeckRecommendOptions()

    preview_requested, args = extract_preview_keyword(args)
    additional, args = extract_addtional_options(args)

    args = extract_live_type(args, options)
    args = extract_random_strategy(args, options, "average", "average")
    args = extract_multilive_options(args, options)
    args = extract_fixed_cards_and_characters(args, options)
    args = extract_card_config(args, options)
    args = extract_target(args, options)

    # 算法
    options.algorithm = "all"
    options.timeout_ms = int(RECOMMEND_TIMEOUT_CFG.get() * 1000)
    if "dfs" in args:
        options.algorithm = "dfs"
        args = args.replace("dfs", "").strip()
        options.timeout_ms = int(SINGLE_ALG_RECOMMEND_TIMEOUT_CFG.get() * 1000)

    # 活动id
    args, preview_resolution, preview_handled = await extract_preview_target_event(
        ctx,
        args,
        options,
        preview_requested=preview_requested,
        match_type="all",
        command_kind="event",
    )
    if not preview_handled:
        args = await extract_target_event_or_simulate_event(ctx, args, options)
        
    # 歌曲id和难度
    args = await extract_music_and_diff(ctx, args, options, "event", options.live_type, additional)

    # 组卡限制
    options.limit = config.get('deck.return_deck_num.multi')

    # 模拟退火设置
    options.sa_options = DeckRecommendSaOptions()
    options.sa_options.max_no_improve_iter = 10000

    return {
        'options': options,
        'last_args': args.strip(),
        'additional': additional,
        'preview_resolution': preview_resolution,
    }

# 从args中提取挑战组卡参数
async def extract_challenge_options(ctx: SekaiHandlerContext, args: str) -> Dict:
    args = ctx.get_args().strip().lower()
    options = DeckRecommendOptions()

    preview_requested, args = extract_preview_keyword(args)
    assert_and_reply(
        not preview_requested,
        "预览仅支持活动组卡和加成组卡",
    )
    additional, args = extract_addtional_options(args)

    args = extract_live_type(args, options)
    options.live_type = 'challenge_auto' if options.live_type == 'auto' else 'challenge'
    random_strategy = 'average' if 'auto' in options.live_type else 'max'
    args = extract_random_strategy(args, options, random_strategy, random_strategy)
    args = extract_fixed_cards_and_characters(args, options)
    args = extract_card_config(args, options)
    args = extract_target(args, options)

    # 算法
    options.algorithm = "all"
    options.timeout_ms = int(RECOMMEND_TIMEOUT_CFG.get() * 1000)
    if "dfs" in args:
        options.algorithm = "dfs"
        args = args.replace("dfs", "").strip()
        options.timeout_ms = int(SINGLE_ALG_RECOMMEND_TIMEOUT_CFG.get() * 1000)
    
    # 指定角色
    options.challenge_live_character_id = None
    segs = args.split()
    full_nickname, part_nickname = None, None
    for seg in segs:
        nickname, rest = extract_nickname_from_args(seg)
        if rest.isdigit():  # 不匹配角色名+数字
            continue
        if not full_nickname and nickname and not rest:
            full_nickname = nickname
        if not part_nickname and nickname:
            part_nickname = nickname 
    # 优先使用完全匹配的昵称
    if full_nickname:
        options.challenge_live_character_id = get_cid_by_nickname(full_nickname)
        args = args.replace(full_nickname, "", 1).strip()
    elif part_nickname:
        options.challenge_live_character_id = get_cid_by_nickname(part_nickname)
        args = args.replace(part_nickname, "", 1).strip()
    ## 不指定角色情况下每个角色都组1个最强卡

    # 歌曲id和难度
    args = await extract_music_and_diff(ctx, args, options, "challenge", options.live_type, additional)

    # 组卡限制
    options.limit = config.get('deck.return_deck_num.challenge')

    # 模拟退火设置
    options.sa_options = DeckRecommendSaOptions()
    if options.challenge_live_character_id is None:
        options.sa_options.run_num = 5  # 不指定角色情况下适当减少模拟退火次数

    return {
        'options': options,
        'last_args': args.strip(),
        'additional': additional,
    }

# 从args中提取长草组卡参数
async def extract_no_event_options(ctx: SekaiHandlerContext, args: str) -> Dict:
    args = ctx.get_args().strip().lower()
    options = DeckRecommendOptions()

    preview_requested, args = extract_preview_keyword(args)
    assert_and_reply(
        not preview_requested,
        "预览仅支持活动组卡和加成组卡",
    )
    additional, args = extract_addtional_options(args)

    args = extract_live_type(args, options)
    args = extract_random_strategy(args, options, "average", "average")
    args = extract_multilive_options(args, options)
    args = extract_fixed_cards_and_characters(args, options)
    args = extract_card_config(args, options)
    args = extract_target(args, options)

    # 算法
    options.algorithm = "all"
    options.timeout_ms = int(NO_EVENT_RECOMMEND_TIMEOUT_CFG.get() * 1000)
    if "dfs" in args:
        options.algorithm = "dfs"
        args = args.replace("dfs", "").strip()
        options.timeout_ms = int(SINGLE_ALG_RECOMMEND_TIMEOUT_CFG.get() * 1000)

    # 活动id
    options.event_id = None
        
    # 歌曲id和难度
    args = await extract_music_and_diff(ctx, args, options, "event", options.live_type, additional)

    # 组卡限制
    options.limit = config.get('deck.return_deck_num.multi')

    # 模拟退火设置
    options.sa_options = DeckRecommendSaOptions()
    options.sa_options.max_no_improve_iter = 50000

    return {
        'options': options,
        'last_args': args.strip(),
        'additional': additional,
    }

# 从args中提取加成组卡参数
async def extract_bonus_options(ctx: SekaiHandlerContext, args: str) -> Dict:
    args = ctx.get_args().strip().lower()
    options = DeckRecommendOptions()

    preview_requested, args = extract_preview_keyword(args)
    additional, args = extract_addtional_options(args)

    options.algorithm = "dfs"
    options.timeout_ms = int(BONUS_RECOMMEND_TIMEOUT_CFG.get() * 1000)
    options.target = "bonus"
    options.live_type = "solo"

    # 卡牌设置
    options.rarity_1_config = NOCHANGE_CARD_CONFIG
    options.rarity_2_config = NOCHANGE_CARD_CONFIG
    options.rarity_3_config = NOCHANGE_CARD_CONFIG
    options.rarity_4_config = NOCHANGE_CARD_CONFIG
    options.rarity_birthday_config = NOCHANGE_CARD_CONFIG

    # 活动id
    args, preview_resolution, preview_handled = await extract_preview_target_event(
        ctx,
        args,
        options,
        preview_requested=preview_requested,
        match_type="full",
        command_kind="bonus",
    )
    if not preview_handled:
        event, wl_cid, args = await extract_target_event(
            ctx, args,
            match_type="full",
            default_return_current=True,
            raise_if_not_found=True,
        )
        options.event_id = event['id']
        options.world_bloom_character_id = wl_cid
        
    # 歌曲id和难度
    await extract_music_and_diff(ctx, "", options, "event", options.live_type, additional)

    # 组卡限制
    options.limit = config.get('deck.return_deck_num.bonus')

    # 目标加成
    try:
        options.target_bonus_list = list(map(int, args.split()))
        assert options.target_bonus_list
    except:
        raise ReplyException("""
使用方式: /加成组卡 加成 其他参数...
例如: /加成组卡 120
""".strip())

    return {
        'options': options,
        'last_args': '',
        'additional': additional,
        'preview_resolution': preview_resolution,
    }

# 从args中提取烤森组卡参数
async def extract_mysekai_options(ctx: SekaiHandlerContext, args: str) -> Dict:
    args = ctx.get_args().strip().lower()
    options = DeckRecommendOptions()

    preview_requested, args = extract_preview_keyword(args)
    assert_and_reply(
        not preview_requested,
        "烤森组卡暂不支持预览",
    )
    additional, args = extract_addtional_options(args)

    options.algorithm = "ga"
    options.timeout_ms = int(RECOMMEND_TIMEOUT_CFG.get() * 1000)
    options.live_type = "mysekai"

    args = extract_fixed_cards_and_characters(args, options)
    args = extract_card_config(args, options, default_nochange=True)

    args = await extract_target_event_or_simulate_event(ctx, args, options)

    # 组卡限制
    options.limit = config.get('deck.return_deck_num.mysekai')

    # 歌曲id和难度
    await extract_music_and_diff(ctx, "", options, "event", "multi", additional)

    return {
        'options': options,
        'last_args': '',
        'additional': additional,
    }


# ======================= 处理逻辑 ======================= #

RECOMMEND_SERVERS_CFG = config.item("deck.servers")
_deckrec_request_id = 0


# 添加OMAKASE音乐
def add_omakase_music(music_metas: list[dict]) -> list[dict]:
    if find_by(music_metas, "id", OMAKASE_MUSIC_ID) is None:
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
            "fever_score": 0,
            "fever_end_time": 0,
            "tap_count": 0,
        }

        music_count = 0
        for item in music_metas:
            if item['difficulty'] in OMAKASE_MUSIC_DIFFS:
                omakase['music_time'] += item['music_time']
                omakase['event_rate'] += item['event_rate']
                omakase['base_score'] += item['base_score']
                omakase['base_score_auto'] += item['base_score_auto']
                for i in range(6):
                    omakase['skill_score_solo'][i] += item['skill_score_solo'][i]
                    omakase['skill_score_auto'][i] += item['skill_score_auto'][i]
                    omakase['skill_score_multi'][i] += item['skill_score_multi'][i]
                omakase['fever_score'] += item['fever_score']
                omakase['fever_end_time'] += item['fever_end_time']
                omakase['tap_count'] += item['tap_count']
                music_count += 1

        omakase['music_time'] /= music_count
        omakase['event_rate'] = int(omakase['event_rate'] / music_count)
        omakase['base_score'] /= music_count
        omakase['base_score_auto'] /= music_count
        for i in range(6):
            omakase['skill_score_solo'][i] /= music_count
            omakase['skill_score_auto'][i] /= music_count
            omakase['skill_score_multi'][i] /= music_count
        omakase['fever_score'] /= music_count
        omakase['fever_end_time'] /= music_count
        omakase['tap_count'] = int(omakase['tap_count'] / music_count)

        for difficulty in ('easy', 'normal', 'hard', 'expert', 'master', 'append'):
            new_omakase = omakase.copy()
            new_omakase['difficulty'] = difficulty
            music_metas.append(new_omakase)
    return music_metas

# 获取deck的hash
def get_deck_hash(deck: RecommendDeck) -> str:
    deck_hash = str(deck.score) + str(deck.total_power) + str(deck.cards[0].card_id)
    return deck_hash

# 打印组卡配置
def log_options(ctx: SekaiHandlerContext, user_id: int, options: DeckRecommendOptions):
    def cardconfig2str(cfg: DeckRecommendCardConfig):
        return f"{(int)(cfg.disable)}{(int)(cfg.level_max)}{(int)(cfg.episode_read)}{(int)(cfg.master_max)}{(int)(cfg.skill_max)}"
    log = "组卡配置: "
    log += f"region={ctx.region}, "
    log += f"uid={user_id}, "
    log += f"type={options.live_type}, "
    log += f"mid={options.music_id}, "
    log += f"mdiff={options.music_diff}, "
    log += f"eid={options.event_id}, "
    log += f"wl_cid={options.world_bloom_character_id}, "
    log += f"challenge_cid={options.challenge_live_character_id}, "
    log += f"limit={options.limit}, "
    log += f"member={options.member}, "
    log += f"rarity1={cardconfig2str(options.rarity_1_config)}, "
    log += f"rarity2={cardconfig2str(options.rarity_2_config)}, "
    log += f"rarity3={cardconfig2str(options.rarity_3_config)}, "
    log += f"rarity4={cardconfig2str(options.rarity_4_config)}, "
    log += f"rarity_bd={cardconfig2str(options.rarity_birthday_config)}, "
    log += f"fixed_cards={options.fixed_cards}"
    logger.info(log)

# 自动组卡实现（批量提交），每批会发送到同一个后端，返回 [Tuple[结果，结果算法来源，Dict[算法: Tuple[耗时，等待时间]]], ...]
async def do_deck_recommend_batch(
    ctx: SekaiHandlerContext, 
    options_list: list[DeckRecommendOptions],
    user_data: bytes,
    servers_override: Optional[list[dict]] = None,
) -> list[Tuple[DeckRecommendResult, List[str], Dict[str, Tuple[timedelta, timedelta]]]]:
    # 获取组卡后端相关信息
    servers = (
        servers_override
        if servers_override is not None
        else RECOMMEND_SERVERS_CFG.get()
    )
    if not servers:
        raise ReplyException("未配置可用的组卡服务")
    server_urls = [s['url'] for s in servers]
    server_weights = [s['weight'] for s in servers]
    server_url_indices = list(range(len(server_urls)))
    server_min_weight = min([w for w in server_weights if w > 0], default=0)
    if server_min_weight <= 0:
        raise ReplyException("未配置可用的组卡服务")
    
    # 负载均衡决定请求后端优先级
    global _deckrec_request_id
    _deckrec_request_id += 1
    server_order = [[] for _ in range(server_min_weight)]
    for i, w in enumerate(server_weights):
        for j in range(w):
            server_order[j % len(server_order)].append(i)
    server_order = [idx for sublist in server_order for idx in sublist]
    select_idx = server_order[_deckrec_request_id % len(server_order)]
    urls = server_urls[select_idx:] + server_urls[:select_idx]
    url_indices = server_url_indices[select_idx:] + server_url_indices[:select_idx]

    # 通用请求函数
    async def req(payload: bytes, url: str) -> dict:
        async with get_client_session().post(url, data=payload) as resp:
            if resp.status != 200:
                msg = f"{resp.status}: "
                raw_detail = ""
                body_text = ""
                try:
                    body_text = await resp.text()
                    # 组卡服务有时会被 WAF / 反代拦截并返回整页 HTML。
                    # 这里不能把原文直接拼进 ReplyException，否则：
                    # 1. 用户会看到整页垃圾 HTML；
                    # 2. 外层 logger.warning(get_exc_desc(e)) 会把日志直接打爆。
                    err_data = loads_json(body_text)
                    if isinstance(err_data, dict) and 'detail' in err_data:
                        raw_detail = err_data['detail']
                    else:
                        raw_detail = err_data
                except Exception:
                    raw_detail = body_text
                msg += summarize_http_error_detail(raw_detail, resp.content_type)
                raise ReplyException(msg)
            return await resp.json()

    # 用户数据负载
    userdata_payload = []
    add_payload_segment(userdata_payload, user_data)
    userdata_payload = build_multiparts_payload(userdata_payload)

    # 组卡请求数据，以及原始options_list的索引映射（用于结果归类）
    recommend_data = { 'region': ctx.region, 'batch_options': [] }
    original_indices = []
    for i, options in enumerate(options_list):
        if options.algorithm == "all": 
            algs = RECOMMEND_ALGS_CFG.get()
        else:
            algs = [options.algorithm]
        for alg in algs:
            opt = options.to_dict()
            opt['algorithm'] = alg
            recommend_data['batch_options'].append(opt)
            original_indices.append(i)
    
    # 按后端优先级进行组卡请求
    errors = {}
    result_list = None
    for url, url_index in zip(urls, url_indices):
        # 向该后端缓存用户数据段
        try:
            res = await req(userdata_payload, url + "/cache_userdata")
            recommend_data['userdata_hash'] = res.get('userdata_hash')
            payload = []
            add_payload_segment(payload, dumps_json(recommend_data, indent=False).encode('utf-8'))
            with ProfileTimer("deckrec.request"):
                result_list = await req(build_multiparts_payload(payload), url + "/recommend")
            break
        except Exception as e:
            logger.warning(f"组卡请求 {url} 失败: {get_exc_desc(e)}")
            errors.setdefault(get_exc_desc(e), []).append(url_index+1)
    
    # 所有后端请求均失败
    if result_list is None:
        error_text = ""
        for err_msg, url_idxs in errors.items():
            error_text += "".join([f"[{url_index}]" for url_index in url_idxs]) + f" {err_msg}\n"
        raise ReplyException(f"请求所有可用的组卡服务失败:\n" + error_text.strip())

    # 结果归类整理
    result_dict = {}
    for original_index, result in zip(original_indices, result_list):
        result_dict.setdefault(original_index, []).append(result)
    
    ret = []
    for index in range(len(options_list)):
        results = result_dict[index]
        # 结果排序去重
        decks: List[RecommendDeck] = []
        cost_and_wait_times = {}
        deck_src_alg = {}
        for resp in results:
            alg = resp['alg']
            cost_time = resp['cost_time']
            wait_time = resp['wait_time']
            result = DeckRecommendResult.from_dict(resp['result'])
            cost_and_wait_times[alg] = (cost_time, wait_time)
            for deck in result.decks:
                deck_hash = get_deck_hash(deck)
                if deck_hash not in deck_src_alg:
                    deck_src_alg[deck_hash] = alg
                    decks.append(deck)
                else:
                    deck_src_alg[deck_hash] += "+" + alg
        def key_func(deck: RecommendDeck):
            if options.live_type == "mysekai":
                return (deck.mysekai_event_point, deck.total_power)
            elif options.target == "score":
                return (deck.score, deck.multi_live_score_up)
            elif options.target == "power":
                return deck.total_power
            elif options.target == "skill":
                return deck.multi_live_score_up
            elif options.target == "bonus":
                return (-deck.event_bonus_rate, deck.score)
        limit = options.limit if options.target != "bonus" else options.limit * len(options.target_bonus_list)
        decks = sorted(decks, key=key_func, reverse=True)[:limit]
        src_algs = [deck_src_alg[get_deck_hash(deck)] for deck in decks]
        res = DeckRecommendResult()
        # 加成组卡的队伍按照加成排序
        if options.target == "bonus":
            for deck in decks:
                deck.cards = sorted(deck.cards, key=lambda x: x.event_bonus_rate, reverse=True)
        res.decks = decks
        ret.append((res, src_algs, cost_and_wait_times))
    return ret

# 构造顶配profile
async def construct_max_profile(ctx: SekaiHandlerContext, max_area_item_level: int | None = None) -> dict:
    try: 
        await ctx.md.mysekai_gates.get()
        has_mysekai = True
    except:
        has_mysekai = False

    p = {
        'userGamedata': {},
        'userDecks': [],
        'userCards': [],
        'userHonors': [],
        "userMysekaiCanvases": [],
        "userCharacters": [],
        "userMysekaiGates": [],
        "userMysekaiFixtureGameCharacterPerformanceBonuses": [],
        "userAreas": [],
    }

    for card in await ctx.md.cards.get():
        release_time = datetime.fromtimestamp(card['releaseAt'] / 1000)
        if release_time > datetime.now():
            continue
        episodes = await ctx.md.card_episodes.find_by("cardId", card['id'], mode='all')
        
        match card['cardRarityType']:
            case "rarity_1": level = 20
            case "rarity_2": level = 30
            case "rarity_3": level = 50
            case "rarity_4": level = 60
            case "rarity_birthday": level = 60

        p["userCards"].append({
            "cardId": card['id'],
            "level": level,
            "skillLevel": 4,
            "masterRank": 5,
            "specialTrainingStatus": "done" if has_after_training(card) else "none",
            "defaultImage": "special_training" if has_after_training(card) else "original",
            "episodes": [
                {
                    "cardEpisodeId": ep['id'],
                    "scenarioStatus": "already_read",
                } for ep in episodes
            ]
        })
        if has_mysekai:
            p['userMysekaiCanvases'].append({
                'cardId': card['id'],
                "quantity": 1,
            })

    for honor in await ctx.md.honors.get():
        if honor.get('levels'):
            p['userHonors'].append({
                "honorId": honor['id'],
                "level": honor['levels'][-1]['level'],
            })

    for cid in range(1, 27):
        p['userCharacters'].append({
            "characterId": cid,
            "characterRank": 120,
        })

    if has_mysekai:
        for gid in range(1, 6):
            p['userMysekaiGates'].append({
                "mysekaiGateId": gid,
                "mysekaiGateLevel": 40,
            })
        
        fixture_chara_bonus = { cid: 0 for cid in range(1, 27) }
        for fixture in await ctx.md.mysekai_fixtures.get():
            bid = fixture.get('mysekaiFixtureGameCharacterGroupPerformanceBonusId')
            if bid:
                cid = (bid - 1) // 3 + 1
                t = (bid - 1) % 3
                if t == 0: fixture_chara_bonus[cid] += 1
                elif t == 1: fixture_chara_bonus[cid] += 3
                else: fixture_chara_bonus[cid] += 6
        for cid in range(1, 27):
            p['userMysekaiFixtureGameCharacterPerformanceBonuses'].append({
                "gameCharacterId": cid,
                "totalBonusRate": min(fixture_chara_bonus[cid], 100)
            })
    
    levels = {}
    for item in await ctx.md.area_item_levels.get():
        item_id = item['areaItemId']
        lv = item['level']
        if max_area_item_level is not None and lv > max_area_item_level:
            continue
        levels[item_id] = max(levels.get(item_id, 0), lv)
    p['userAreas'].append({
        "userAreaStatus": {},
        "areaItems": [
            {
                "areaItemId": item_id,
                "level": lv,
            } for item_id, lv in levels.items()
        ]
    })

    return p

# 根据用户数据推荐挑战组卡歌曲
async def recommend_challenge_music(
    ctx: SekaiHandlerContext,
    profile: dict | None,
) -> tuple[int, str] | None:
    if not config.get('deck.challenge_music_auto_recommend.enabled'):
        return None
    if not profile or not profile.get('userMusicResults'):
        return None

    # 统计各难度各等级fc数量
    fc_count: dict[str, dict[int, int]] = {}
    for music in await get_valid_musics(ctx, leak=False):
        for diff in DIFF_COLORS:
            mid = music['id']
            level = (await get_music_diff_info(ctx, mid)).level.get(diff)
            if not level: 
                continue
            results = find_by(profile['userMusicResults'], "musicId", mid, mode='all') 
            results = find_by(results, 'musicDifficultyType', diff, mode='all') + find_by(results, 'musicDifficulty', diff, mode='all')
            if results:
                full_combo, all_prefect = False, False
                for item in results:
                    full_combo = full_combo or item["fullComboFlg"]
                    all_prefect = all_prefect or item["fullPerfectFlg"]
                if full_combo or all_prefect:
                    fc_count.setdefault(diff, {}).setdefault(level, 0)
                    fc_count[diff][level] += 1
    # 各等级后缀和
    for diff in DIFF_COLORS:
        if count := fc_count.get(diff):
            for level in range(40, 1):
                if level + 1 not in count:
                    continue
                count.setdefault(level, 0)
                count[level] += count[level + 1]
    # 根据规则进行推荐
    for rule in config.get('deck.challenge_music_auto_recommend.rules'):
        mid, diff = rule['music']
        if not await is_valid_music(ctx, mid, leak=False, diff=diff):
            continue
        ok = True
        for req_key, req_count in rule['fc_requires'].items():
            req_diff, req_level = req_key.split('_')
            if fc_count.get(req_diff, {}).get(int(req_level), 0) < req_count:
                ok = False
                break
        if ok:
            return (mid, diff)
    return None


# 合成自动组卡图片
async def compose_deck_recommend_image(
    ctx: SekaiHandlerContext, 
    qid: int,
    options: DeckRecommendOptions,
    last_args: str,
    additional: dict,
    preview_resolution: Optional[PreviewEventResolution] = None,
) -> Image.Image:
    # ---------------------------- 判断组卡类型方便后续处理 ---------------------------- #

    NO_MUSIC_TYPES = ["bonus", "wl_bonus", "mysekai"]

    is_wl = options.world_bloom_character_id or options.event_id == 180
    is_preview = preview_resolution is not None
    jp_ctx = SekaiHandlerContext.from_region('jp')

    if options.live_type == "mysekai":
        recommend_type = "mysekai"
    elif options.target == "bonus":
        if is_wl:
            recommend_type = "wl_bonus"
        else:
            recommend_type = "bonus"
    elif options.live_type in ["challenge", "challenge_auto"]:
        if options.challenge_live_character_id:
            recommend_type = "challenge"
        else:
            recommend_type = "challenge_all"
    elif options.world_bloom_event_turn:
        recommend_type = "wl_fake"
    elif options.event_id:
        if is_wl:
            recommend_type = "wl"
        else:
            recommend_type = "event"
    else:
        if options.event_unit:
            recommend_type = "unit_attr"
        else:
            recommend_type = "no_event"

    war_prepare = additional.get('war_prepare', False)
    # Preview 后端返回的活动 PT 与原生组卡结果同构，备战只依赖该 PT、
    # 火耗和周回参数，因此可以复用同一套估算。
    if war_prepare:
        assert_and_reply(
            recommend_type not in ("challenge", "challenge_all", "no_event", "bonus", "wl_bonus", "mysekai"),
            "备战模式仅支持有活动PT结果的组卡",
        )
        assert_and_reply(additional.get('boost') is not None, "备战模式必须指定火耗参数，例如\"5火\"")
        assert_and_reply(
            additional.get('target_pt') is not None,
            "你还没有输入目标PT！例：目标300万/300w（请以1或万为单位，方便程序解析）",
        )
        assert_and_reply(not additional.get('music_compare', False), "备战模式暂不支持和歌曲比较同时使用")

    # ---------------------------- 处理额外参数 ---------------------------- #
            
    # 是否是顶配租卡
    use_max_profile = additional.get('max_profile', False)
    use_sub_max_profile = additional.get('sub_max_profile', False)
    if use_max_profile:
        profile = await construct_max_profile(ctx)
        uid = None
    elif use_sub_max_profile:
        profile = await construct_max_profile(ctx, max_area_item_level=15)
        uid = None
    else:
        # 用户信息
        with ProfileTimer("deckrec.get_detailed_profile"):
            profile, pmsg = await get_detailed_profile(
                ctx, 
                qid, 
                filter=get_detailed_profile_card_filter(
                    'userGamedata',
                    'userDecks',
                    'userCards',
                    'userHonors',
                    "userMysekaiCanvases",
                    "userCharacters",
                    "userMysekaiGates",
                    "userMysekaiFixtureGameCharacterPerformanceBonuses",
                    "userAreas", 
                    'userChallengeLiveSoloDecks',
                    'userChallengeLiveSoloHighScoreRewards',
                    'userChallengeLiveSoloStages',
                    'userChallengeLiveSoloResults',
                    'userMusicResults',
                ),
                strict=False,
                raise_exc=True, ignore_hide=True)

            # ================================ Suite 核心字段兜底 ================================ #
            # 组卡后续会大量依赖 `userGamedata.userId`、`userCards`、`userDecks` 等核心字段。
            # 即使上游 OAuth / Public-API 某次结构波动，至少也要在这里抛出可读错误，
            # 不能直接落成 KeyError 让用户完全看不懂发生了什么。
            assert_and_reply(
                isinstance(profile.get('userGamedata'), dict) and profile['userGamedata'].get('userId'),
                format_suite_missing_fields_error(ctx, profile, ['userGamedata']),
            )
            uid = profile['userGamedata']['userId']

    original_usercards = profile['userCards']
    assumed_cards_context: AssumedCardsContext | None = None
    assumed_usercards_by_id: dict[int, dict] = {}
    jp_cards_by_id: dict[int, dict] = {}
    # 组合卡牌过滤
    unit_filter = additional.get('unit_filter', None)
    if unit_filter:
        profile['userCards'] = [
            uc for uc in profile['userCards']
            if await get_unit_by_card_id(ctx, uc['cardId'], return_support=(unit_filter != 'piapro')) == unit_filter
        ]
    # 属性卡牌过滤
    attr_filter = additional.get('attr_filter', None)
    if attr_filter:
        profile['userCards'] = [
            uc for uc in profile['userCards']
            if (await ctx.md.cards.find_by_id(uc['cardId']))['attr'] == attr_filter
        ]
    # 排除卡牌
    excluded_cards = additional.get('excluded_cards', [])
    if excluded_cards:
        profile['userCards'] = [
            uc for uc in profile['userCards']
            if uc['cardId'] not in excluded_cards
        ]

    # 使用当前队伍
    use_current_deck = additional.get('use_current_deck', False)
    if use_current_deck:
        assert_and_reply(recommend_type != 'challenge_all', "需要指定挑战组卡角色才能使用\"当前\"参数")
        if recommend_type == 'challenge':
            deck = find_by(profile.get('userChallengeLiveSoloDecks', []), "characterId", options.challenge_live_character_id)
            assert_and_reply(deck, "找不到你的该角色的当前挑战卡组（更新当前挑战卡组需要抓包）")
            cards = []
            if deck.get('leader'): cards.append(deck['leader'])
            if deck.get('support1'): cards.append(deck['support1'])
            if deck.get('support2'): cards.append(deck['support2'])
            if deck.get('support3'): cards.append(deck['support3'])
            if deck.get('support4'): cards.append(deck['support4'])
            if len(cards)!= 5:
                raise ReplyException("你的该角色的当前挑战卡组不足5张，无法使用\"当前\"参数（更新当前挑战卡组需要抓包）")
            options.fixed_cards = cards
            options.fixed_characters = None
            options.best_skill_as_leader = False
        else:
            basic_profile = await get_basic_profile(
                ctx, get_player_bind_id(ctx), 
                use_cache=False, use_remote_cache=False,
            )
            options.fixed_cards = [basic_profile['userDeck'][f'member{i}'] for i in range(1, 6)]
            options.fixed_characters = None
            options.best_skill_as_leader = False
            # 转移basic_profile中的卡到profile中
            for bp_card in basic_profile['userCards']:
                if p_card := find_by(profile['userCards'], 'cardId', bp_card['cardId']):
                    p_card.update(bp_card)
                else:
                    # suite中没有该卡，提示需要抓包更新
                    raise ReplyException(f"当前卡组中的卡牌 {bp_card['cardId']} 不在Suite数据中，请更新抓包数据")

    # ================================ 显式未来卡假设 ================================ #
    # 必须在 CN 过滤/当前队伍处理完成后注入，而且先复制列表，避免临时卡污染
    # 用于头像和玩家信息绘制的原始 Suite 对象。
    if preview_resolution is not None:
        profile['userCards'] = list(profile['userCards'])
        future_fixed_ids = [
            card_id
            for card_id in (options.fixed_cards or [])
            if card_id in preview_resolution.future_card_ids
        ]
        jp_episodes_by_card_id: dict[int, list[dict]] = {}
        for card_id in future_fixed_ids:
            card = await jp_ctx.md.cards.find_by_id(card_id)
            if card:
                jp_cards_by_id[card_id] = card
            jp_episodes_by_card_id[card_id] = await jp_ctx.md.card_episodes.find_by(
                "cardId",
                card_id,
                mode="all",
            )
        # 单卡设置优先叠加在对应稀有度设置上，保证临时 Suite 与实际下发给
        # 原生库的满技、满破和剧情状态一致。
        rarity_configs = {
            "rarity_1": options.rarity_1_config,
            "rarity_2": options.rarity_2_config,
            "rarity_3": options.rarity_3_config,
            "rarity_4": options.rarity_4_config,
            "rarity_birthday": options.rarity_birthday_config,
        }
        single_configs = {
            int(single.card_id): single
            for single in (options.single_card_configs or [])
        }
        assumed_config_overrides: dict[int, dict[str, bool]] = {}
        for card_id, card in jp_cards_by_id.items():
            rarity_config = rarity_configs.get(card.get("cardRarityType"))
            single_config = single_configs.get(card_id)
            assumed_config_overrides[card_id] = {
                key: bool(
                    getattr(rarity_config, key, False)
                    or getattr(single_config, key, False)
                )
                for key in ("episode_read", "master_max", "skill_max")
            }
        try:
            max_assumed_cards = int(config.get(
                "deck.cn_jp_preview.max_assumed_cards"
            ))
        except Exception:
            max_assumed_cards = 5
        try:
            assumed_cards_context = inject_explicit_assumed_cards(
                profile,
                fixed_card_ids=options.fixed_cards or [],
                cn_catalog_card_ids={
                    card["id"]
                    for card in await ctx.md.cards.get()
                    if isinstance(card.get("id"), int)
                },
                future_card_ids=set(preview_resolution.future_card_ids),
                jp_cards_by_id=jp_cards_by_id,
                jp_episodes_by_card_id=jp_episodes_by_card_id,
                max_assumed_cards=max_assumed_cards,
                card_config_overrides=assumed_config_overrides,
            )
        except PreviewCardError as exc:
            raise ReplyException(str(exc)) from exc
        assumed_usercards_by_id = {
            user_card["cardId"]: user_card
            for user_card in profile["userCards"]
            if user_card.get("cardId") in assumed_cards_context.assumed_card_ids
        }

    # ================================ 终章目标与牌子模拟 ================================ #
    # 终章的支援和额外加成都跟队长角色绑定，因此要先把目标角色固化，再决定后续裁卡池策略。
    final_chapter_target_cid = await apply_final_chapter_target_character(ctx, options, use_current_deck)

    assert_and_reply(
        not additional.get('final_chapter_badge_sim', False) or options.event_id == 180,
        "“tk牌”仅支持终章活动组卡使用",
    )
    assert_and_reply(
        not additional.get('final_chapter_badge_sim', False) or final_chapter_target_cid is not None,
        "使用“tk牌”时需要先指定终章目标角色，例如“终章 miku tk牌”",
    )

    await apply_final_chapter_badge_simulation(
        ctx,
        profile,
        final_chapter_target_cid,
        bool(additional.get('final_chapter_badge_sim', False)),
    )

    # ================================ 终章下发参数纠偏 ================================ #
    # event180 在 MasterData 里只有 finale 章节，没有“按角色拆开的 worldBloom chapter”。
    # 因此这里不能再把目标角色继续塞进 world_bloom_character_id 下发给后端，
    # 否则服务端会误走“按章节找角色”的分支并直接报 chapter not found。
    #
    # 终章真正的冲榜目标应由队长角色决定：
    # 1. 固定卡牌时，第一张固定卡就是队长；
    # 2. 只指定角色时，前面已经转成 fixed_characters 约束；
    # 3. tk牌则通过临时注入 honor 来模拟，不需要额外 chapter 参数。
    if options.event_id == 180:
        options.world_bloom_character_id = None

    # 终章指定固定卡牌时，需要保证“第一张固定卡就是队长角色”，因此直接走 DFS。
    if options.event_id == 180 and options.fixed_cards:
        options.algorithm = "dfs"

    # 如果卡组完全固定则只需要跑一种算法；但 WL / 终章仍然需要整份卡池来计算支援加成，
    # 否则固定 5 张前排后，支援候选只剩前排本身，去重后会直接变成 0。
    is_deck_fixed = options.fixed_cards and len(options.fixed_cards) == 5 or use_current_deck
    if is_deck_fixed:
        options.algorithm = "dfs"
        if not is_wl:
            profile['userCards'] = [
                uc for uc in profile['userCards']
                if uc['cardId'] in options.fixed_cards
            ]

    # 检查是否在未使用固定队伍情况下指定技能顺序
    if not is_deck_fixed:
        assert_and_reply(options.skill_order_choose_strategy != "specific", 
                         "仅在使用固定队伍（例如添加\"当前\"参数）时可指定特定技能顺序")

    # 歌曲比较相关
    music_compare = False
    if additional.get('music_compare'):
        options.best_skill_as_leader = False    # 固定位置
        music_compare = True
        music_diffs_to_compare: list[tuple[int, str, str]] = additional.get('music_diffs_to_compare', [])
        music_compare_show_num = len(music_diffs_to_compare) if music_diffs_to_compare else MUSIC_COMPARE_DEFAULT_MUSIC_NUM

        if not music_diffs_to_compare:
            # 必须至少固定歌曲或固定卡组，（非固定卡组的情况每首都组一遍开销太大）
            assert_and_reply(is_deck_fixed, f"""
如果不限定要比较的歌曲，则必须固定一个卡组！
1. 固定5个卡牌ID:
/指令 ... 歌曲比较 #1 2 3 4 5
2. 固定为你的主队配置(实时更新):
/指令 ... 歌曲比较 当前
3. 限定比较的歌曲（难度默认ma）:
/指令 ... 歌曲比较 龙 虾ex 群青apd
4. 限定比较的歌曲并固定卡组:
/指令 ... 歌曲比较 龙 虾ex #1 2 3 4 5
""".strip())
            # 没有指定比较的歌曲，则通过musicmeta计算出5张卡技能加分全100%的情况的前排候选歌曲，
            musicmetas = await musicmetas_json.get()
            music_values = []
            is_multi = options.live_type in ['multi', 'cheerful']
            is_auto = options.live_type in ['auto', 'challenge_auto']
            for item in musicmetas:
                music_id = item['music_id']
                diff = item['difficulty']
                if not await is_valid_music(ctx, music_id, False, diff):
                    continue
                value = item['base_score'] if not is_auto else item['base_score_auto']
                for i in range(6):
                    key = 'skill_score_solo'
                    if is_multi: key = 'skill_score_multi'
                    if is_auto: key = 'skill_score_auto'
                    value += item[key][i] * (1.8 if is_multi else 1.0)
                if is_multi:
                    value += item['fever_score'] * 0.5
                if recommend_type in ['event', 'wl', 'wl_fake', 'unit_attr'] and options.target == 'score':
                    value *= item['event_rate'] / 100.0
                music_values.append((value, music_id, diff))
            music_values = sorted(music_values, key=lambda x: x[0], reverse=True)[:MUSIC_COMPARE_CANDIDATE_MUSIC_NUM]
            for _, mid, diff in music_values:
                music_diffs_to_compare.append((mid, diff, ""))

    # 挑战组卡自动推荐歌曲
    use_recommended_challenge_music = False
    if options.live_type == "challenge" and additional.get('use_default_music'):
        if res := await recommend_challenge_music(ctx, profile):
            options.music_id, options.music_diff = res
            use_recommended_challenge_music = True

    # 体力加成
    boost = additional.get('boost', None)
    if options.live_type not in ('multi', 'auto', 'solo') or options.target != 'score':
        boost = None
    if boost is not None:
        boost_bonus = BOOST_BONUS_DICT.get(boost, 1)

    # 区域道具等级
    area_item_level = additional.get('area_item_level', None)
    if area_item_level is not None:
        levels = {}
        for item in await ctx.md.area_item_levels.get():
            item_id = item['areaItemId']
            lv = item['level']
            if lv > area_item_level:
                continue
            levels[item_id] = max(levels.get(item_id, 0), lv)
        # 检查区服还没有开放等级上限
        for item_id, lv in levels.items():
            if lv < area_item_level:
                raise ReplyException(f"{get_region_name(ctx.region)}区域道具等级最多为{lv}")
        # 已存在的区域道具等级覆盖
        for area in profile['userAreas']:
            for area_item in area['areaItems']:
                item_id = area_item['areaItemId']
                if item_id in levels:
                    area_item['level'] = max(area_item['level'], levels[item_id])
                    del levels[item_id]
        # 不存在的添加
        profile['userAreas'].append({
            "userAreaStatus": {},
            "areaItems": [
                {
                    "areaItemId": item_id,
                    "level": lv,
                } for item_id, lv in levels.items()
            ]
        })
        

    # ---------------------------- 调用组卡服务 ---------------------------- #

    options.region = ctx.region
    log_options(ctx, uid, options)

    # 准备用户数据
    user_data = dump_bytes_json(profile)  
    # 还原profile避免画头像问题
    profile['userCards'] = original_usercards

    # 准备批次组卡参数
    all_options = []
    if recommend_type == "challenge_all":
        # 挑战组卡没有指定角色情况下，每角色组1个最强
        assert_and_reply(not music_compare, f"挑战组卡必须指定一个角色才能进行歌曲比较")
        for cid in range(1, 26 + 1):
            options.challenge_live_character_id = cid
            options.limit = 1
            all_options.append(DeckRecommendOptions(options))
        options.challenge_live_character_id = None
    elif music_compare:
        # 歌曲比较
        options.limit = 1
        for mid, diff, _ in music_diffs_to_compare:
            options.music_id = mid
            options.music_diff = diff
            all_options.append(DeckRecommendOptions(options))
    else:
        # 正常组卡
        all_options = [options]

    # ================================ 组卡后端路由 ================================ #
    servers_override = None
    if preview_resolution is not None:
        preview_state = preview_manager.get_state()
        assert_and_reply(
            preview_state.status == PreviewStatus.READY
            and preview_state.scope_fingerprint == preview_resolution.scope_fingerprint,
            "预览数据刚刚更新，请重新发送指令",
        )
        servers_override = [
            {
                "url": server["url"],
                "weight": server["weight"],
            }
            for server in preview_manager.ready_servers()
        ]
        assert_and_reply(
            servers_override,
            "预览组卡暂时不可用，请稍后再试",
        )
    elif preview_manager.enabled() and options.event_id and (
        official_entry := preview_manager.official_entry(options.event_id)
    ):
        # 已由 preview 粘性切回的活动，只允许路由到确认加载正式 CN 指纹的节点。
        acked_urls = {
            str(url).rstrip("/")
            for url in official_entry.get("acked_servers", [])
        }
        servers_override = [
            server
            for server in RECOMMEND_SERVERS_CFG.get()
            if str(server.get("url", "")).rstrip("/") in acked_urls
        ]
        assert_and_reply(
            servers_override,
            "国服活动数据正在同步，请稍后再试",
        )

    # 调用组卡并合并批次结果
    cost_times, wait_times = {}, {}
    result_decks = []
    result_algs = []
    for res, algs, cost_and_wait_times in await do_deck_recommend_batch(
        ctx,
        all_options,
        user_data,
        servers_override=servers_override,
    ):
        result_decks.extend(res.decks)
        result_algs.extend(algs)
        for alg, (cost, wait) in cost_and_wait_times.items():
            cost_times.setdefault(alg, []).append(cost)
            wait_times.setdefault(alg, []).append(wait)
    for alg in cost_and_wait_times:
        cost_times[alg] = sum(cost_times[alg]) / len(cost_times[alg])
        wait_times[alg] = sum(wait_times[alg]) / len(wait_times[alg])

    # 歌曲比较模式还要额外进行排序
    if music_compare:
        result_music_decks = list(zip(music_diffs_to_compare, result_decks))
        result_music_decks.sort(key=lambda x: x[1].score, reverse=True)
        result_music_decks = result_music_decks[:music_compare_show_num]
        result_decks = [d for _, d in result_music_decks]
        music_diffs_to_compare = [md for md, _ in result_music_decks]

    if war_prepare:
        result_decks = result_decks[:1]
        result_algs = result_algs[:1]

    if preview_resolution is not None and assumed_cards_context is not None:
        try:
            validate_preview_result_cards(
                result_decks,
                assumed_cards_context,
                fixed_card_ids=options.fixed_cards or [],
            )
        except PreviewCardError as exc:
            logger.error(
                "预演组卡结果集合越界: "
                f"event={preview_resolution.event_id} "
                f"fp={preview_resolution.scope_fingerprint} "
                f"error={get_exc_desc(exc)}"
            )
            raise ReplyException("预演结果安全校验失败，本次结果已作废") from exc

    # ---------------------------- 绘图数据获取 ---------------------------- #

    if not music_compare:
        # 获取一般情况音乐标题和封面
        if options.music_id == OMAKASE_MUSIC_ID:
            music_title = "おまかせ（所有歌曲平均）"
            music_cover = ctx.static_imgs.get('omakase.png')
        else:
            music = await jp_ctx.md.musics.find_by_id(options.music_id)
            music_title = truncate(music['title'], 20)
            music_title += f" ({options.music_diff.upper()})"
            music_cover = await get_music_cover_thumb(jp_ctx, options.music_id)

    # ================================ 备战结果计算 ================================ #
    # 这一块直接复用公式：基于当前最优活动PT结果，继续估算
    # 单局PT、时速、局数、预计时间和火耗。
    war_prepare_info = None
    if war_prepare and result_decks:
        deck = result_decks[0]
        boost = additional['boost']
        boost_bonus = BOOST_BONUS_DICT[boost]
        single_play_pt = int(deck.score * boost_bonus)

        manual_rounds_per_hour = additional.get('rounds_per_hour')
        if manual_rounds_per_hour is not None:
            rounds_per_hour = manual_rounds_per_hour
            rounds_source = "玩家设置"
        else:
            musicmetas = add_omakase_music(await musicmetas_json.get())
            meta = find_by_predicate(
                musicmetas,
                lambda x: x['music_id'] == options.music_id and x['difficulty'] == options.music_diff,
            )
            assert_and_reply(meta is not None, "找不到当前歌曲的周回数据，无法计算备战信息")
            live_type_to_leaderboard = {
                'solo': 'solo',
                'multi': 'multi',
                'auto': 'auto',
                'cheerful': 'multi',
            }
            leaderboard_live_type = live_type_to_leaderboard.get(options.live_type)
            assert_and_reply(leaderboard_live_type is not None, "当前LIVE类型暂不支持备战模式")
            play_interval = LEADERBOARD_LIVETYPE_PLAY_INTERVAL[leaderboard_live_type]
            rounds_per_hour = 3600 / (meta['music_time'] + play_interval)
            rounds_source = "系统默认"

        current_pt = additional.get('current_pt', 0)
        target_pt = additional['target_pt']
        remain_pt = max(0, target_pt - current_pt)
        required_games = math.ceil(remain_pt / single_play_pt) if remain_pt > 0 else 0
        estimated_hours = required_games / rounds_per_hour if rounds_per_hour > 0 else 0
        hourly_pt = int(single_play_pt * rounds_per_hour)
        total_boost_cost = required_games * boost

        war_prepare_info = {
            'current_pt': current_pt,
            'target_pt': target_pt,
            'remain_pt': remain_pt,
            'single_play_pt': single_play_pt,
            'rounds_per_hour': rounds_per_hour,
            'rounds_source': rounds_source,
            'manual_rounds_per_hour': manual_rounds_per_hour,
            'hourly_pt': hourly_pt,
            'required_games': required_games,
            'estimated_hours': estimated_hours,
            'total_boost_cost': total_boost_cost,
            'boost': boost,
        }

    # 获取活动banner和标题
    live_name = "协力"
    event_id = options.event_id
    if recommend_type in ["event", "wl", "bonus", "wl_bonus", "mysekai"] and event_id:
        event_asset_ctx = jp_ctx if is_preview else ctx
        event = (
            preview_resolution.rule_event
            if preview_resolution is not None
            else await ctx.md.events.find_by_id(event_id)
        )
        if event:
            event_banner = await get_event_banner_img(event_asset_ctx, event)
            event_title = event['name']
            if event['eventType'] == 'cheerful_carnival':
                live_name = "5v5" 
        else:
            # 预填充终章的情况
            event_banner, event_title = None, ""

    # 团队属性组卡指定5v5
    if recommend_type == "unit_attr" and options.event_type == "cheerful_carnival":
        live_name = "5v5"
        
    # 获取挑战角色名字和头像
    chara_name = None
    if recommend_type == "challenge":
        chara = await ctx.md.game_characters.find_by_id(options.challenge_live_character_id)
        chara_name = chara.get('firstName', '') + chara.get('givenName', '')
        chara_icon = get_chara_icon_by_chara_id(chara['id'])

    # 获取WL角色名字和头像
    wl_chara_name = None
    wl_display_cid = final_chapter_target_cid if event_id == 180 else options.world_bloom_character_id
    if wl_display_cid:
        wl_chara = await ctx.md.game_characters.find_by_id(wl_display_cid)
        if wl_chara:
            wl_chara_name = wl_chara.get('firstName', '') + wl_chara.get('givenName', '')
            wl_chara_icon = get_chara_icon_by_chara_id(wl_chara['id'])
        else:
            wl_chara_name = ""
            wl_chara_icon = None

    # 获取指定团名和属性的icon和logo
    unit_logo, attr_icon = None, None
    if options.event_unit and options.event_attr:
        unit_logo = get_unit_logo(options.event_unit)
        attr_icon = get_attr_icon(options.event_attr)

    # 获取卡组卡牌缩略图
    draw_eventbonus = recommend_type in ["bonus", "wl_bonus"]

    # ================================ 结果卡牌元数据 ================================ #
    # Preview 后端能返回显式固定的 JP 未实装卡。后续缩略图和角色判断必须复用
    # JP 元数据，不能重新去 CN cards 表查询并把合法的 None 当成字典使用。
    result_card_masterdata_cache: dict[int, dict] = {}

    async def _get_result_card_masterdata(card_id: int) -> dict:
        if cached := result_card_masterdata_cache.get(card_id):
            return cached

        assumed = bool(
            assumed_cards_context
            and card_id in assumed_cards_context.assumed_card_ids
        )
        if assumed:
            cn_card = None
            jp_card = (
                jp_cards_by_id.get(card_id)
                or await jp_ctx.md.cards.find_by_id(card_id)
            )
        else:
            cn_card = await ctx.md.cards.find_by_id(card_id)
            jp_card = None

        try:
            selected = select_result_card_masterdata(
                card_id,
                assumed_cards_context,
                cn_card=cn_card,
                jp_card=jp_card,
            )
        except PreviewCardError as exc:
            raise ReplyException(str(exc)) from exc

        result = dict(selected)
        result_card_masterdata_cache[card_id] = result
        return result

    async def _get_thumb(draw_ctx, card, pcard):
        try: 
            custom_text = None
            if draw_eventbonus:
                bonus = pcard.get('eventBonus', 0)
                if abs(bonus - int(bonus)) < 0.01:
                    bonus = int(bonus)
                custom_text = f"+{bonus}%"
            return await get_card_full_thumbnail(
                draw_ctx,
                card,
                pcard=pcard,
                custom_text=custom_text,
            )
        except: 
            return UNKNOWN_IMG
    card_imgs, card_keys = [], []
    for deck in result_decks:
        for deckcard in deck.cards:
            assumed = bool(
                assumed_cards_context
                and deckcard.card_id in assumed_cards_context.assumed_card_ids
            )
            card_ctx = jp_ctx if assumed else ctx
            card = await _get_result_card_masterdata(deckcard.card_id)
            usercard = (
                assumed_usercards_by_id.get(deckcard.card_id)
                if assumed
                else find_by(profile['userCards'], 'cardId', deckcard.card_id)
            )
            pcard = {
                'cardId': deckcard.card_id,
                'defaultImage': deckcard.default_image,                                 # 默认图片跟随组卡结果
                'specialTrainingStatus': usercard.get('specialTrainingStatus', 'none') if usercard else 'none', # 稀有度图标绘制跟随原本卡组
                'level': deckcard.level,
                'masterRank': deckcard.master_rank,
                'eventBonus': deckcard.event_bonus_rate,
            }
            card_key = f"{deckcard.card_id}_{deckcard.default_image}"
            if card_key not in card_keys:
                card_keys.append(card_key)
                card_imgs.append(_get_thumb(card_ctx, card, pcard))
    card_imgs = await asyncio.gather(*card_imgs)
    card_imgs = { key : img for key, img in zip(card_keys, card_imgs) }

    # 获取挑战live额外分数信息
    challenge_score_dlt = []
    if recommend_type in ["challenge", "challenge_all"]:
        try: challenge_live_info = await get_user_challenge_live_info(ctx, profile)
        except: challenge_live_info = {}
        for deck in result_decks:
            card_id = deck.cards[0].card_id
            chara_id = (await _get_result_card_masterdata(card_id))['characterId']
            _, high_score, _, _ = challenge_live_info.get(chara_id, (None, 0, None, None))
            challenge_score_dlt.append(deck.score - high_score)

    # ---------------------------- 绘图 ---------------------------- #
        
    with Canvas(bg=SEKAI_BLUE_BG).set_padding(BG_PADDING) as canvas:
        with VSplit().set_content_align('lt').set_item_align('lt').set_sep(16).set_padding(16):
            if uid is not None:
                await get_detailed_profile_card(ctx, profile, pmsg)

            with VSplit().set_content_align('lt').set_item_align('lt').set_sep(16).set_padding(16).set_bg(roundrect_bg()):
                # 标题
                with VSplit().set_content_align('lb').set_item_align('lb').set_sep(16).set_padding(16).set_bg(roundrect_bg()):
                    title = ""

                    if recommend_type == "mysekai":
                        if event_id:
                            title += f"烤森活动#{event_id}组卡"
                        else:
                            title += f"烤森模拟活动组卡"
                    elif recommend_type in ['challenge', 'challenge_all']: 
                        title += "每日挑战组卡"
                        if options.live_type == "challenge_auto":
                            title += "(AUTO)"
                    elif recommend_type in ['bonus', 'wl_bonus']:
                        if recommend_type == "bonus":
                            title += f"活动#{event_id}加成组卡"
                        elif recommend_type == "wl_bonus":
                            title += f"WL活动#{event_id}加成组卡"
                    else:
                        if recommend_type == "event":
                            title += f"活动#{event_id}组卡"
                        elif recommend_type == "wl":
                            if wl_chara_name:
                                title += f"WL活动#{event_id}组卡"
                            else:
                                title += f"WL终章活动组卡"
                        elif recommend_type == "wl_fake":
                            title += f"第{options.world_bloom_event_turn}轮WL模拟组卡"
                        elif recommend_type == "unit_attr":
                            title += f"团队+颜色模拟活动组卡"
                        elif recommend_type == "no_event":
                            title += f"无活动组卡"
                    
                        if options.live_type == "multi":
                            title += f"({live_name})"
                        elif options.live_type == "solo":
                            title += "(单人)"
                        elif options.live_type == "auto":
                            title += "(AUTO)"

                    if preview_resolution is not None:
                        # Preview 只追加模式标记，活动类型、编号和 Live 描述继续
                        # 完全复用原组卡标题，避免产生另一套视觉命名规则。
                        title = format_preview_result_title(title)
                    
                    score_name = "PT"
                    if recommend_type in ["challenge", "challenge_all", "no_event"]:
                        score_name = "分数"  

                    with HSplit().set_content_align('l').set_item_align('l').set_sep(16):
                        if recommend_type in ["event", "wl", "bonus", "wl_bonus", "mysekai"] and options.event_id:
                            if event_banner:
                                ImageBox(event_banner, size=(None, 50))
                            else:
                                title = event_title + " " + title

                        TextBox(title, TextStyle(font=DEFAULT_BOLD_FONT, size=30, color=(50, 50, 50)), use_real_line_count=True)

                        if recommend_type == "challenge":
                            ImageBox(chara_icon, size=(None, 50))
                            TextBox(f"{chara_name}", TextStyle(font=DEFAULT_BOLD_FONT, size=30, color=(70, 70, 70)))
                        if wl_chara_name:
                            wl_desc = "目标" if event_id == 180 else "章节"
                            ImageBox(wl_chara_icon, size=(None, 50))
                            TextBox(f"{wl_chara_name} {wl_desc}", TextStyle(font=DEFAULT_BOLD_FONT, size=30, color=(70, 70, 70)))
                        if unit_logo and attr_icon:
                            ImageBox(unit_logo, size=(None, 60))
                            ImageBox(attr_icon, size=(None, 50))
                        
                        if use_max_profile:
                            TextBox(f"({get_region_name(ctx.region)}顶配)", TextStyle(font=DEFAULT_BOLD_FONT, size=30, color=(50, 50, 50)))
                        if use_sub_max_profile:
                            TextBox(f"({get_region_name(ctx.region)}次顶配)", TextStyle(font=DEFAULT_BOLD_FONT, size=30, color=(50, 50, 50)))

                    if any([
                        unit_filter, attr_filter, 
                        excluded_cards, 
                        options.multi_live_score_up_lower_bound, 
                        options.keep_after_training_state,
                    ]):
                        with HSplit().set_content_align('l').set_item_align('l').set_sep(16):
                            setting_style = TextStyle(font=DEFAULT_BOLD_FONT, size=24, color=(50, 50, 50))
                            TextBox("卡组设置:", setting_style)
                            if unit_filter or attr_filter:
                                TextBox(f"仅", setting_style)
                                if unit_filter: ImageBox(get_unit_logo(unit_filter), size=(None, 40))
                                if attr_filter: ImageBox(get_attr_icon(attr_filter), size=(None, 35))
                                TextBox(f"上场", setting_style)
                            if excluded_cards:
                                TextBox(f"排除 {','.join(map(str, excluded_cards))}", setting_style)
                            if options.multi_live_score_up_lower_bound:
                                TextBox(f"实效≥{int(options.multi_live_score_up_lower_bound)}%", setting_style)
                            if options.keep_after_training_state:
                                TextBox(f"禁用双技能自动切换", setting_style)
                            
                    if recommend_type in ["bonus", "wl_bonus"]:
                        TextBox(f"该功能需要输入活动加成而不是要控的PT", TextStyle(font=DEFAULT_BOLD_FONT, size=24, color=(255, 50, 50)))
                        TextBox(f"友情提醒：控分前请核对加成和体力设置", TextStyle(font=DEFAULT_BOLD_FONT, size=24, color=(255, 50, 50)))
                        if recommend_type == "wl_bonus":
                            TextBox(f"WL仅支持自动组主队，支援队请自行配置", TextStyle(font=DEFAULT_FONT, size=24, color=(50, 50, 50)))
                    
                    if recommend_type not in NO_MUSIC_TYPES and not music_compare:
                        with HSplit().set_content_align('l').set_item_align('l').set_sep(16):
                            if last_args:
                                TextBox(f"{last_args} → ", TextStyle(font=DEFAULT_BOLD_FONT, size=26, color=(70, 70, 70)))
                            with Frame().set_size((50, 50)):
                                if options.music_id != OMAKASE_MUSIC_ID:
                                    Spacer(w=50, h=50).set_bg(FillBg(fill=DIFF_COLORS[options.music_diff])).set_offset((6, 6))
                                    ImageBox(music_cover, size=(50, 50))
                                else:
                                    ImageBox(music_cover, size=(50, 50), shadow=True)
                            TextBox(music_title, TextStyle(font=DEFAULT_BOLD_FONT, size=26, color=(70, 70, 70)))
                            if use_recommended_challenge_music:
                                TextBox(f"*根据游玩记录自动推荐", TextStyle(font=DEFAULT_FONT, size=20, color=(70, 70, 70)))

                    if recommend_type not in ["bonus", "wl_bonus", "mysekai"]:
                        skill_text_style = TextStyle(font=DEFAULT_BOLD_FONT, size=20, color=(70, 70, 70))
                        with HSplit().set_content_align('l').set_item_align('c').set_sep(16):
                            with HSplit().set_content_align('l').set_item_align('c').set_sep(2):
                                TextBox("技能顺序:", skill_text_style)
                                if options.skill_order_choose_strategy == 'average':
                                    TextBox("⚖️平均情况", skill_text_style)
                                elif options.skill_order_choose_strategy == 'max':
                                    TextBox("🌟最优顺序", skill_text_style)
                                elif options.skill_order_choose_strategy == 'min':
                                    TextBox("🥀最差顺序", skill_text_style)
                                elif options.skill_order_choose_strategy == 'specific':
                                    skill_order = options.specific_skill_order
                                    TextBox(f"{''.join([str(s + 1) for s in skill_order])}", skill_text_style)

                            with HSplit().set_content_align('l').set_item_align('c').set_sep(2):
                                TextBox("BloomFes花前技能吸取:", skill_text_style)
                                if options.skill_reference_choose_strategy == 'average':
                                    TextBox("⚖️平均值", skill_text_style)
                                elif options.skill_reference_choose_strategy == 'max':
                                    TextBox("🌟最大值", skill_text_style)
                                elif options.skill_reference_choose_strategy == 'min':
                                    TextBox("🥀最小值", skill_text_style)
                    
                    info_text = ""

                    if preview_resolution is not None:
                        info_text += (
                            f"活动规则：JP-{preview_resolution.event_id}"
                            "｜玩家数据：CN Suite\n"
                        )
                        info_text += (
                            "按日服活动规则与当前国服账号数据预演，"
                            "请以国服上线后的实际规则为准。\n"
                        )

                    if last_args:
                        arg_unit, args = extract_unit(last_args)
                        arg_attr, args = extract_card_attr(last_args)
                        arg_nickname, args = extract_nickname_from_args(last_args)
                        if arg_unit or arg_attr:
                            info_text += "检测到你的歌曲查询中包含团名或颜色，可能是参数格式不正确\n"
                            info_text += "如果你想指定仅包含某个团名或颜色的卡牌请用: 纯mmj 纯绿\n"
                            info_text += "如果你想组某个团名颜色加成的模拟活动请使用“/组卡”\n"
                        if arg_nickname and not args.strip().isdigit():
                            info_text += "检测到你的歌曲查询中包含角色昵称，可能是参数格式不正确\n"
                            info_text += "如果你想指定固定角色请用: #角色1 角色2...\n"

                    if use_max_profile:
                        info_text += "\"顶配\"为该服截止当前的全卡满养成配置(并非基于你的卡组计算)\n"
                    if use_sub_max_profile:
                        info_text += "\"次顶配\"为该服截止当前的全卡满养成道具15级配置(并非基于你的卡组计算)\n"
                    if use_current_deck:
                        info_text += "活动组卡的“当前”队伍无需抓包更新，挑战组卡则需要抓包更新\n"
                    if area_item_level:
                        info_text += f"所有区域道具等级已提升为至少{area_item_level}级\n"

                    if info_text:  
                        TextBox(info_text.strip(), TextStyle(font=DEFAULT_BOLD_FONT, size=24, color=(200, 75, 75)), use_real_line_count=True)

                # 表格
                gh, vsp, voffset = 120, 12, 18

                # ================================ 备战信息展示 ================================ #
                # 作为组卡结果前的附加信息块展示，不改动原有组卡表格结构。
                if war_prepare_info is not None:
                    rounds_text = (
                        f"{war_prepare_info['rounds_per_hour']:.0f}"
                        if abs(war_prepare_info['rounds_per_hour'] - round(war_prepare_info['rounds_per_hour'])) < 0.05
                        else f"{war_prepare_info['rounds_per_hour']:.1f}".rstrip('0').rstrip('.')
                    )
                    prep_key_style = TextStyle(font=DEFAULT_BOLD_FONT, size=30, color=(88, 92, 118))
                    prep_value_style = TextStyle(font=DEFAULT_BOLD_FONT, size=30, color=(40, 40, 40))
                    prep_note_style = TextStyle(font=DEFAULT_BOLD_FONT, size=26, color=(88, 92, 118))

                    def prep_pair(icon: str, label: str, value: str):
                        with HSplit().set_content_align('l').set_item_align('c').set_sep(0):
                            TextBox(f"{icon} {label}", prep_key_style)
                            TextBox(value, prep_value_style)

                    with VSplit().set_content_align('lt').set_item_align('lt').set_sep(10).set_padding(16).set_bg(roundrect_bg()):
                        if war_prepare_info['current_pt'] > 0:
                            with HSplit().set_content_align('l').set_item_align('lt').set_sep(28):
                                with VSplit().set_content_align('lt').set_item_align('lt').set_sep(10):
                                    prep_pair("🙌", "当前PT:", format_wan_number(war_prepare_info['current_pt']))
                                    prep_pair("📉", "差值:", format_wan_number(war_prepare_info['remain_pt']))
                                    prep_pair("🎮", "预计局数:", str(war_prepare_info['required_games']))
                                with VSplit().set_content_align('lt').set_item_align('lt').set_sep(10):
                                    prep_pair("🎯", "目标PT:", format_wan_number(war_prepare_info['target_pt']))
                                    prep_pair("♻️", "周回数:", f"{war_prepare_info['rounds_source']}({rounds_text})")
                                    prep_pair("⏰", "预计时间:", format_hhmm_short(war_prepare_info['estimated_hours']))
                                with VSplit().set_content_align('lt').set_item_align('lt').set_sep(10):
                                    Spacer(h=34)
                                    prep_pair("🚀", "时速:", format_wan_number(war_prepare_info['hourly_pt']))
                                    prep_pair("🔋", "预计火耗:", str(war_prepare_info['total_boost_cost']))
                            with HSplit().set_content_align('l').set_item_align('c').set_sep(0):
                                TextBox("🔥 按", prep_note_style)
                                TextBox(str(war_prepare_info['boost']), prep_value_style)
                                TextBox("火计算单局PT", prep_note_style)
                        else:
                            with HSplit().set_content_align('l').set_item_align('lt').set_sep(28):
                                with VSplit().set_content_align('lt').set_item_align('lt').set_sep(10):
                                    prep_pair("🎯", "目标PT:", format_wan_number(war_prepare_info['target_pt']))
                                    prep_pair("🎮", "预计局数:", str(war_prepare_info['required_games']))
                                    with HSplit().set_content_align('l').set_item_align('c').set_sep(0):
                                        TextBox("🔥 按", prep_note_style)
                                        TextBox(str(war_prepare_info['boost']), prep_value_style)
                                        TextBox("火计算单局PT", prep_note_style)
                                with VSplit().set_content_align('lt').set_item_align('lt').set_sep(10):
                                    prep_pair("♻️", "周回数:", f"{war_prepare_info['rounds_source']}({rounds_text})")
                                    prep_pair("⏰", "预计时间:", format_hhmm_short(war_prepare_info['estimated_hours']))
                                with VSplit().set_content_align('lt').set_item_align('lt').set_sep(10):
                                    prep_pair("🚀", "时速:", format_wan_number(war_prepare_info['hourly_pt']))
                                    prep_pair("🔋", "预计火耗:", str(war_prepare_info['total_boost_cost']))

                with VSplit().set_content_align('c').set_item_align('c').set_sep(16).set_padding(16).set_bg(roundrect_bg()):
                    if len(result_decks) > 0:
                        with HSplit().set_content_align('c').set_item_align('c').set_sep(16).set_padding(0):
                            th_style1 = TextStyle(font=DEFAULT_BOLD_FONT, size=28, color=(0, 0, 0))
                            th_style2 = TextStyle(font=DEFAULT_BOLD_FONT, size=28, color=(75, 75, 75))
                            th_main_sign = '∇'
                            tb_style = TextStyle(font=DEFAULT_BOLD_FONT, size=24, color=(70, 70, 70))

                            # 歌曲比较添加额外歌曲列
                            if music_compare:
                                with VSplit().set_content_align('c').set_item_align('c').set_sep(vsp).set_padding(8):
                                    TextBox("歌曲", th_style2).set_h(gh // 2).set_content_align('c')
                                    Spacer(h=6)
                                    for (mid, diff, marg), deck in zip(music_diffs_to_compare, result_decks):
                                        music = await jp_ctx.md.musics.find_by_id(mid)
                                        music_title = music['title'] + f" ({diff.upper()})"
                                        with VSplit().set_content_align('c').set_item_align('c').set_sep(8).set_padding(0).set_h(gh):
                                            with Frame().set_content_align('c'):
                                                Spacer(w=64, h=64).set_bg(FillBg(fill=DIFF_COLORS[diff])).set_offset((3, 3))
                                                ImageBox(await get_music_cover_thumb(jp_ctx, mid), size=(64, 64)).set_offset((-3, -3))
                                            text = f"{mid}"
                                            if marg: text = f"{truncate(marg, 8)} → " + text
                                            TextBox(text, TextStyle(font=DEFAULT_FONT, size=15, color=(75, 75, 75)))

                            # 分数
                            if recommend_type not in ["bonus", "wl_bonus"]:
                                with VSplit().set_content_align('c').set_item_align('c').set_sep(vsp).set_padding(8):
                                    target_score = options.target == "score"
                                    text = score_name + th_main_sign if target_score else score_name
                                    style = th_style1 if target_score else th_style2
                                    with Frame().set_h(gh // 2).set_content_align('c'):
                                        TextBox(text, style)
                                        if boost is not None:
                                            TextBox(f"{boost}🔥(x{boost_bonus})", TextStyle(font=DEFAULT_FONT, size=18, color=(75, 75, 75))) \
                                                .set_content_align('c').set_offset((0, 28))
                                    Spacer(h=6)
                                    for i, (deck, alg) in enumerate(zip(result_decks, result_algs)):
                                        with Frame().set_content_align('rb'):
                                            alg_offset = 0
                                            # 挑战分数差距
                                            if recommend_type in ['challenge', 'challenge_all']: 
                                                alg_offset = 20
                                                dlt = challenge_score_dlt[i]
                                                color = (50, 150, 50) if dlt > 0 else (150, 50, 50)
                                                TextBox(f"{dlt:+d}", TextStyle(font=DEFAULT_FONT, size=15, color=color)).set_offset((0, -8-voffset*2))
                                            # 算法
                                            TextBox(alg.upper(), TextStyle(font=DEFAULT_FONT, size=12, color=(125, 125, 125))).set_offset((0, -8-voffset*2+alg_offset))
                                            # 分数
                                            score = deck.score
                                            if recommend_type == "no_event":
                                                score = deck.live_score 
                                            elif recommend_type == "mysekai":
                                                score = deck.mysekai_event_point
                                            if boost is not None:
                                                score = int(score * boost_bonus)
                                            with Frame().set_content_align('c'):
                                                TextBox(str(score), tb_style).set_h(gh).set_content_align('c').set_offset((0, -voffset))

                            # 卡片
                            with VSplit().set_content_align('c').set_item_align('c').set_sep(vsp).set_padding(8):
                                TextBox("卡组", th_style2).set_h(gh // 2).set_content_align('c')
                                Spacer(h=6)
                                for deck in result_decks:
                                    with HSplit().set_content_align('c').set_item_align('c').set_sep(8).set_padding(0):
                                        for card in deck.cards:
                                            card_id = card.card_id
                                            character_id = (
                                                await _get_result_card_masterdata(card_id)
                                            )['characterId']
                                            event_bonus = card.event_bonus_rate
                                            ep1_read, ep2_read = card.episode1_read, card.episode2_read
                                            slv, sup = card.skill_level, int(card.skill_score_up)

                                            with VSplit().set_content_align('c').set_item_align('c').set_sep(4).set_padding(0).set_h(gh):
                                                with Frame().set_content_align('rt'):
                                                    card_key = f"{card_id}_{card.default_image}"
                                                    ImageBox(card_imgs[card_key], size=(None, 80))
                                                    if options.fixed_cards and card_id in options.fixed_cards \
                                                    or options.fixed_characters and character_id in options.fixed_characters:
                                                        TextBox(str(card_id), TextStyle(font=DEFAULT_FONT, size=10, color=WHITE)) \
                                                            .set_bg(RoundRectBg((200, 50, 50, 200), 2)).set_offset((-2, 0)).set_text_offset((0, -2))
                                                    else:
                                                        TextBox(str(card_id), TextStyle(font=DEFAULT_FONT, size=10, color=(75, 75, 75))) \
                                                            .set_bg(RoundRectBg((255, 255, 255, 200), 2)).set_offset((-2, 0)).set_text_offset((0, -2))
                                                    if card.has_canvas_bonus:
                                                        ImageBox(ctx.static_imgs.get(f"mysekai/icon_canvas.png"), size=(11, 11)) \
                                                                .set_offset((-32, 65))

                                                info_bg = RoundRectBg((255, 255, 255, 150), 2)
                                                with HSplit().set_content_align('c').set_item_align('c').set_sep(3).set_padding(0):
                                                    TextBox(f"SLv.{slv}", TextStyle(font=DEFAULT_FONT, size=12, color=(50, 50, 50))).set_bg(info_bg)
                                                    TextBox(f"↑{sup}%", TextStyle(font=DEFAULT_FONT, size=12, color=(50, 50, 50))).set_bg(info_bg)
                                                
                                                with HSplit().set_content_align('c').set_item_align('c').set_sep(3).set_padding(0):
                                                    show_event_bonus = event_bonus > 0
                                                    if show_event_bonus:
                                                        event_bonus_str = f"+{event_bonus:.1f}%" if int(event_bonus) != event_bonus else f"+{int(event_bonus)}%"
                                                        TextBox(event_bonus_str, TextStyle(font=DEFAULT_FONT, size=12, color=(50, 50, 50))).set_bg(info_bg)
                                                    read_fg, read_bg = (50, 150, 50, 255), (255, 255, 255, 255)
                                                    noread_fg, noread_bg = (150, 50, 50, 255), (255, 255, 255, 255)
                                                    none_fg, none_bg = (255, 255, 255, 255), (255, 255, 255, 255)
                                                    ep1_fg = none_fg if ep1_read is None else (read_fg if ep1_read else noread_fg)
                                                    ep1_bg = none_bg if ep1_read is None else (read_bg if ep1_read else noread_bg)
                                                    ep2_fg = none_fg if ep2_read is None else (read_fg if ep2_read else noread_fg)
                                                    ep2_bg = none_bg if ep2_read is None else (read_bg if ep2_read else noread_bg)
                                                    TextBox("前" if show_event_bonus else "前篇", TextStyle(font=DEFAULT_FONT, size=12, color=ep1_fg)).set_bg(info_bg)
                                                    TextBox("后" if show_event_bonus else "后篇", TextStyle(font=DEFAULT_FONT, size=12, color=ep2_fg)).set_bg(info_bg)

                            # 加成
                            if recommend_type not in ["challenge", "challenge_all", "no_event"]:
                                with VSplit().set_content_align('c').set_item_align('c').set_sep(vsp).set_padding(8):
                                    TextBox("加成", th_style2).set_h(gh // 2).set_content_align('c')
                                    Spacer(h=6)
                                    for deck in result_decks:
                                        if is_wl:
                                            bonus = f"{deck.event_bonus_rate:.1f}+{deck.support_deck_bonus_rate:.1f}%"
                                            total = f"{deck.event_bonus_rate+deck.support_deck_bonus_rate:.1f}%"
                                        else:
                                            bonus = None
                                            total = f"{deck.event_bonus_rate:.1f}%"
                                        with Frame().set_content_align('rb'):
                                            if bonus is not None:
                                                TextBox(bonus, TextStyle(font=DEFAULT_FONT, size=14, color=(150, 150, 150))).set_offset((0, -6-voffset*2))
                                            with Frame().set_content_align('c'):
                                                TextBox(total, tb_style).set_h(gh).set_content_align('c').set_offset((0, -voffset))

                            # 实效
                            if options.live_type in ['multi', 'cheerful']:
                                with VSplit().set_content_align('c').set_item_align('c').set_sep(vsp).set_padding(8):
                                    target_skill = options.target == "skill"
                                    text = "实效" + th_main_sign if target_skill else "实效"
                                    style = th_style1 if target_skill else th_style2
                                    TextBox(text, style).set_h(gh // 2).set_content_align('c')
                                    Spacer(h=6)
                                    for deck in result_decks:
                                        with Frame().set_content_align('rb'):
                                            if options.multi_live_teammate_score_up is not None:
                                                teammate_text = f"队友 {int(options.multi_live_teammate_score_up)}"
                                                TextBox(teammate_text, TextStyle(font=DEFAULT_FONT, size=14, color=(125, 125, 125))).set_offset((0, -8-voffset*2))
                                            with Frame().set_content_align('c'):
                                                TextBox(f"{deck.multi_live_score_up:.1f}%", tb_style).set_h(gh).set_content_align('c').set_offset((0, -voffset))

                            # 综合力和算法
                            if recommend_type not in ["bonus", "wl_bonus"]:
                                with VSplit().set_content_align('c').set_item_align('c').set_sep(vsp).set_padding(8):
                                    target_power = options.target == "power"
                                    text = "综合力" + th_main_sign if target_power else "综合力"
                                    style = th_style1 if target_power else th_style2
                                    TextBox(text, style).set_h(gh // 2).set_content_align('c')
                                    Spacer(h=6)
                                    for deck in result_decks:
                                        with Frame().set_content_align('rb'):
                                            if options.multi_live_teammate_power is not None:
                                                teammate_text = f"队友 {int(options.multi_live_teammate_power)}"
                                                TextBox(teammate_text, TextStyle(font=DEFAULT_FONT, size=14, color=(125, 125, 125))).set_offset((0, -8-voffset*2))
                                            with Frame().set_content_align('c'):
                                                TextBox(str(deck.total_power), tb_style).set_h(gh).set_content_align('c').set_offset((0, -voffset))
                    # 找不到结果
                    else:
                        TextBox("未找到符合条件的卡组", TextStyle(font=DEFAULT_BOLD_FONT, size=26, color=(255, 50, 50)))

                # 说明
                with VSplit().set_content_align('lt').set_item_align('lt').set_sep(4):
                    tip_style = TextStyle(font=DEFAULT_FONT, size=16, color=(20, 20, 20))
                    TextBox(f"功能移植并修改自33Kit https://3-3.dev/sekai/deck-recommend 算错概不负责", tip_style)
                    def tt(t: float) -> str:
                        return f"{t*1000:.0f}ms" if t < 1 else f"{t:.2f}s"
                    alg_and_cost_text = "本次组卡使用算法: "
                    for alg in cost_times:
                        alg_and_cost_text += f"{alg.upper()}-{RECOMMEND_ALG_NAMES[alg]} (等待{tt(wait_times[alg])}/耗时{tt(cost_times[alg])}) + "
                    alg_and_cost_text = alg_and_cost_text[:-3]
                    TextBox(alg_and_cost_text, tip_style)
                    TextBox(f"若发现组卡漏掉最优解可指定固定卡牌再尝试，发送\"{ctx.original_trigger_cmd}help\"获取详细帮助", tip_style)

    add_watermark(canvas)

    with ProfileTimer("deckrec.draw"):
        img = await canvas.get_img()

    return img


# ======================= 指令处理 ======================= #

# 活动组卡
pjsk_event_deck = SekaiCmdHandler([
    "/pjsk event card", "/pjsk event deck", "/pjsk deck", 
    "/活动组卡", "/活动组队", "/活动卡组", "/活动配队",
    "/组卡", "/组队", "/配队", 
    "/指定属性组卡", "/指定属性组队", "/指定属性卡组", "/指定属性配队",
    "/模拟组卡", "/模拟配队", "/模拟组队", "/模拟卡组",
])
pjsk_event_deck.check_cdrate(cd).check_wblist(gbl)
@pjsk_event_deck.handle()
async def _(ctx: SekaiHandlerContext):
    with ProfileTimer("deckrec.total"):
        await ctx.asend_reply_msg(await get_image_cq(
            await compose_deck_recommend_image(
                ctx, ctx.user_id, 
                **(await extract_event_options(ctx, ctx.get_args()))
            ),
            low_quality=True,
        ))


# 挑战组卡
pjsk_challenge_deck = SekaiCmdHandler([
    "/pjsk challenge card", "/pjsk challenge deck",
    "/挑战组卡", "/挑战组队", "/挑战卡组", "/挑战配队",
])
pjsk_challenge_deck.check_cdrate(cd).check_wblist(gbl)
@pjsk_challenge_deck.handle()
async def _(ctx: SekaiHandlerContext):
    return await ctx.asend_reply_msg(await get_image_cq(
        await compose_deck_recommend_image(
            ctx, ctx.user_id,
            **(await extract_challenge_options(ctx, ctx.get_args()))
        ),
        low_quality=True,
    ))


# 长草组卡
pjsk_no_event_deck = SekaiCmdHandler([
    "/pjsk no event deck", "/pjsk best deck",
    "/长草组卡", "/长草组队", "/长草卡组", "/长草配队", 
    "/最强卡组", "/最强组卡", "/最强组队", "/最强配队",
])
pjsk_no_event_deck.check_cdrate(cd).check_wblist(gbl)
@pjsk_no_event_deck.handle()
async def _(ctx: SekaiHandlerContext):
    return await ctx.asend_reply_msg(await get_image_cq(
        await compose_deck_recommend_image(
            ctx, ctx.user_id,
            **(await extract_no_event_options(ctx, ctx.get_args()))
        ),
        low_quality=True,
    ))


# 加成组卡
pjsk_bonus_deck = SekaiCmdHandler([
    "/pjsk bonus deck", "/pjsk bonus card",
    "/加成组卡", "/加成组队", "/加成卡组", "/加成配队",
    "/控分组卡", "/控分组队", "/控分卡组", "/控分配队",
])
pjsk_bonus_deck.check_cdrate(cd).check_wblist(gbl)
@pjsk_bonus_deck.handle()
async def _(ctx: SekaiHandlerContext):
    return await ctx.asend_reply_msg(await get_image_cq(
        await compose_deck_recommend_image(
            ctx, ctx.user_id,
            **(await extract_bonus_options(ctx, ctx.get_args()))
        ),
        low_quality=True,
    ))


# 烤森组卡
mysekai_deck = SekaiCmdHandler([
    "/mysekai deck", "/pjsk mysekai deck",
    "/烤森组卡", "/烤森组队", "/烤森卡组", "/烤森配队",
    "/ms组卡", "/ms组队", "/ms卡组", "/ms配队",
])
mysekai_deck.check_cdrate(cd).check_wblist(gbl)
@mysekai_deck.handle()
async def _(ctx: SekaiHandlerContext):
    return await ctx.asend_reply_msg(await get_image_cq(
        await compose_deck_recommend_image(
            ctx, ctx.user_id,
            **(await extract_mysekai_options(ctx, ctx.get_args()))
        ),
        low_quality=True,
    ))


# 实效计算
pjsk_score_up = CmdHandler([
    "/实效", "/倍率", "/时效", "/pjsk score up",
], logger)
pjsk_score_up.check_cdrate(cd).check_wblist(gbl)
@pjsk_score_up.handle()
async def _(ctx: SekaiHandlerContext):
    try:
        args = ctx.get_args().strip().split()
        values = list(map(float, args))
        assert len(values) == 5
    except:
        raise ReplyException(f"使用方式: {ctx.trigger_cmd} 100 100 100 100 100") 
    res = values[0] + (values[1] + values[2] + values[3] + values[4]) / 5.
    return await ctx.asend_reply_msg(f"实效: {res:.1f}%")



# ======================= 定时任务 ======================= #

DECKREC_DATA_UPDATE_INTERVAL_CFG = config.item('deck.data_update_interval_seconds')


# ================================ 组卡服务MasterData同步 ================================ #

async def get_deckrec_masterdata_paths(ctx: SekaiHandlerContext) -> List[str]:
    """
    获取组卡服务依赖的 MasterData 文件路径。
    这里必须和 deck_recommender 服务端实际消费的文件集合保持一致，
    否则单个文件热更新时会出现版本号一致但内容不一致的情况。
    """
    specs = get_deck_masterdata_specs(
        include_mysekai=ctx.region in MYSEKAI_REGIONS,
        include_wl_limited_bonus=bool(await ctx.md.events.find_by_id(180)),
    )
    masterdata_tasks = [
        getattr(ctx.md, spec.attribute).get_path()
        for spec in specs
    ]
    paths = await asyncio.gather(*masterdata_tasks)
    validate_resolved_paths(specs, paths)
    return paths


async def get_deckrec_masterdata_path_map(
    ctx: SekaiHandlerContext,
    *,
    include_mysekai: bool,
    include_wl_limited_bonus: bool,
) -> dict[str, str]:
    """按共享规格返回文件名到来源路径的映射，供 preview builder 冻结快照。"""

    specs = get_deck_masterdata_specs(
        include_mysekai=include_mysekai,
        include_wl_limited_bonus=include_wl_limited_bonus,
    )
    paths = await asyncio.gather(*[
        getattr(ctx.md, spec.attribute).get_path()
        for spec in specs
    ])
    validate_resolved_paths(specs, paths)
    path_map = {
        spec.filename: path
        for spec, path in zip(specs, paths)
    }
    assert set(path_map) == set(get_preview_expected_filenames())
    return path_map


def calc_deckrec_masterdata_fingerprint(masterdata_paths: List[str]) -> str:
    """
    基于文件名、大小和修改时间生成同步指纹。
    MasterData 可能在版本号不变的情况下做单文件热修，因此不能只依赖版本号。
    """
    digest = md5()
    for path in masterdata_paths:
        stat = os.stat(path)
        digest.update(f"{os.path.basename(path)}:{stat.st_size}:{stat.st_mtime_ns}\n".encode('utf-8'))
    return digest.hexdigest()


# ================================ Preview来源与正式切回核验 ================================ #

async def _validate_cn_event_rules_for_cutover(
    ctx: SekaiHandlerContext,
    event: dict,
) -> bool:
    """检查 CN 已出现的活动是否具备正式组卡所需的类型化闭包。"""

    event_id = event["id"]
    if event.get("eventType") not in {"marathon", "world_bloom"}:
        return False
    bonuses = await ctx.md.event_deck_bonuses.find_by(
        "eventId",
        event_id,
        mode="all",
    )
    if not bonuses:
        return False
    cards = await ctx.md.cards.get()
    card_ids = {
        card["id"]
        for card in cards
        if isinstance(card.get("id"), int)
    }
    unit_ids = {
        item["id"]
        for item in await ctx.md.game_character_units.get()
    }
    valid_attrs = {
        item["attr"]
        for item in cards
    }
    for bonus in bonuses:
        unit_id = bonus.get("gameCharacterUnitId")
        attr = bonus.get("cardAttr")
        rate = bonus.get("bonusRate")
        if unit_id is not None and unit_id not in unit_ids:
            return False
        if attr is not None and attr not in valid_attrs:
            return False
        if not isinstance(rate, (int, float)) or not math.isfinite(rate) or rate < 0:
            return False

    # ================================ 活动卡闭包 ================================ #
    # 活动行和加成行可能先于卡牌/剧情分批落地；eventCards 为空不能被当成
    # “无需校验”，否则会在 CN 卡池尚未完整时永久切回正式服务。
    event_cards = await ctx.md.event_cards.find_by(
        "eventId",
        event_id,
        mode="all",
    ) or []
    if not event_cards:
        return False

    event_card_ids = [event_card.get("cardId") for event_card in event_cards]
    if any(not isinstance(card_id, int) for card_id in event_card_ids):
        return False

    episode_groups = await asyncio.gather(*[
        ctx.md.card_episodes.find_by("cardId", card_id, mode="all")
        for card_id in event_card_ids
    ])
    episode_card_ids = {
        card_id
        for card_id, episodes in zip(event_card_ids, episode_groups)
        if episodes
    }
    if not validate_event_card_closure(
        event_cards,
        cards,
        await ctx.md.skills.get(),
        episode_card_ids,
    ):
        return False

    if event["eventType"] == "world_bloom":
        chapters = [
            chapter
            for chapter in await ctx.md.world_blooms.get()
            if chapter.get("eventId") == event_id
        ]
        if not chapters:
            return False
        character_ids = {
            character["id"]
            for character in await ctx.md.game_characters.get()
        }
        chapter_keys: set[tuple[int, int]] = set()
        for chapter in chapters:
            chapter_no = chapter.get("chapterNo")
            character_id = chapter.get("gameCharacterId")
            chapter_key = (chapter_no, character_id)
            if (
                not isinstance(chapter_no, int)
                or chapter_no <= 0
                or character_id not in character_ids
                or chapter_key in chapter_keys
            ):
                return False
            chapter_keys.add(chapter_key)

        # ================================ WL支援规则闭包 ================================ #
        # 原生探针只能证明“算得出来”；这里还要确认 WL3 使用的全局倍率表
        # 结构完整且每个嵌套倍率有效，避免旧表返回一个看似正常的有限数。
        support_bonus_rows, different_attr_rows = await asyncio.gather(
            ctx.md.world_bloom_support_deck_bonuses.get(),
            ctx.md.world_bloom_different_attribute_bonuses.get(),
        )
        if not validate_world_bloom_support_tables(
            support_bonus_rows,
            different_attr_rows,
        ):
            return False

        limited = [
            item
            for item in await (
                ctx.md.world_bloom_support_deck_unit_event_limited_bonuses.get()
            )
            if item.get("eventId") == event_id
        ]
        for item in limited:
            rate = item.get("bonusRate")
            if (
                item.get("cardId") not in card_ids
                or item.get("gameCharacterId") not in character_ids
                or not isinstance(rate, (int, float))
                or not math.isfinite(rate)
                or rate < 0
            ):
                return False
        exchange_summaries, event_items = await asyncio.gather(
            ctx.md.event_exchange_summaries.find_by(
                "eventId",
                event_id,
                mode="all",
            ),
            ctx.md.event_items.find_by(
                "eventId",
                event_id,
                mode="all",
            ),
        )
        if not exchange_summaries or not event_items:
            return False
    return True


async def _get_official_loaded_servers(
    expected_fingerprint: str,
) -> list[str]:
    """只返回 DB 与全部 worker 都确认当前 CN 指纹的正式节点。"""

    async def check(server: dict) -> Optional[str]:
        url = str(server["url"]).rstrip("/")
        try:
            async with get_client_session().get(
                url + "/data_status",
                params={"region": "cn"},
            ) as response:
                if response.status != 200:
                    return None
                status = await response.json()
            loaded = status.get("loaded_fingerprints")
            if (
                status.get("instance_role") != "official"
                or status.get("maintenance")
                or status.get("data_healthy") is not True
                or status.get("stored_fingerprint") != expected_fingerprint
                or not isinstance(loaded, dict)
                or not loaded
                or set(loaded.values()) != {expected_fingerprint}
            ):
                return None
            return url
        except Exception:
            return None

    unique_servers = {
        str(server["url"]).rstrip("/"): server
        for server in RECOMMEND_SERVERS_CFG.get()
        if int(server.get("weight", 0)) > 0
    }
    results = await asyncio.gather(*[
        check(server)
        for server in unique_servers.values()
    ])
    return sorted({url for url in results if url})


async def provide_preview_builder_source() -> dict:
    """
    等待资源管理器异步准备 CN/JP 路径，并组装一次性 builder 请求。

    大 JSON 读取、哈希、合并、canary 与压缩全部留给 builder 子进程。
    """

    cn_ctx = SekaiHandlerContext.from_region("cn")
    jp_ctx = SekaiHandlerContext.from_region("jp")
    cn_version, jp_version = await asyncio.gather(
        cn_ctx.md.get_version(),
        jp_ctx.md.get_version(),
    )
    cn_paths, jp_paths = await asyncio.gather(
        get_deckrec_masterdata_path_map(
            cn_ctx,
            include_mysekai=True,
            include_wl_limited_bonus=True,
        ),
        get_deckrec_masterdata_path_map(
            jp_ctx,
            include_mysekai=True,
            include_wl_limited_bonus=True,
        ),
    )
    current_cn_fingerprint = calc_deckrec_masterdata_fingerprint(
        list(cn_paths.values())
    )
    current_jp_fingerprint = calc_deckrec_masterdata_fingerprint(
        list(jp_paths.values())
    )
    current_manifest = preview_manager.get_state().manifest

    # ================================ 缓存版本号兜底 ================================ #
    # 资源源暂时不可达时，WebMasterData 仍可能从完整本地缓存提供全部文件，
    # 但 get_version() 会返回 0.0.0.0。版本号只作可读标签，真正的数据身份由
    # 逐文件哈希和来源指纹保证，因此这里改用稳定的缓存指纹，不能用裸 assert
    # 把可用缓存误判为初始化失败。
    def effective_version(
        region: str,
        version: Any,
        source_fingerprint: str,
    ) -> str:
        version_text = str(version)
        if version_text != DEFAULT_VERSION:
            return version_text
        fallback = f"cache:{source_fingerprint}"
        if current_manifest.get(
            f"{region}_masterdata_version"
        ) != fallback:
            logger.warning(
                f"{region.upper()} MasterData 版本源暂不可用，"
                f"preview 使用本地缓存指纹 {source_fingerprint[:8]}"
            )
        return fallback

    cn_version_text = effective_version(
        "cn",
        cn_version,
        current_cn_fingerprint,
    )
    jp_version_text = effective_version(
        "jp",
        jp_version,
        current_jp_fingerprint,
    )

    # 触发 WebJson 的异步下载/磁盘兜底，builder 只读取已经稳定落盘的缓存。
    await musicmetas_json.get()
    musicmetas_update_time = await musicmetas_json.get_update_time()
    musicmetas_path = musicmetas_json.file_cache_path
    assert musicmetas_path and os.path.isfile(musicmetas_path)

    try:
        allowed_event_types = config.get(
            "deck.cn_jp_preview.allowed_event_types"
        )
    except Exception:
        allowed_event_types = ["marathon", "world_bloom"]
    try:
        event_allowlist = config.get("deck.cn_jp_preview.event_allowlist")
    except Exception:
        event_allowlist = []
    try:
        builder_owner = bool(config.get("deck.cn_jp_preview.builder_owner"))
    except Exception:
        builder_owner = True

    output_root = "data/sekai/deckrec/rulesets/cn_jp_preview_v1"
    builder_request = {
        "cn_paths": cn_paths,
        "jp_paths": jp_paths,
        "output_root": output_root,
        "cn_masterdata_version": cn_version_text,
        "jp_masterdata_version": jp_version_text,
        "include_mysekai": True,
        "include_wl_limited_bonus": True,
        "musicmetas_path": musicmetas_path,
        "musicmetas_update_ts": int(musicmetas_update_time.timestamp()),
        "native_package_build_id": get_native_package_build_id(),
        "allowed_event_types": allowed_event_types,
        # 原生索引对未来活动表存在未公开的跨活动闭包约束。路由白名单只限制
        # 玩家入口，不能用来裁剪底层 MasterData；否则两场活动的稀疏规则集会冷加载失败。
        "event_allowlist": [],
        "route_event_allowlist": event_allowlist,
        "run_canaries": True,
    }

    # ================================ 正式活动粘性激活候选 ================================ #
    builder_request["cn_source_fingerprint"] = current_cn_fingerprint
    builder_request["jp_source_fingerprint"] = current_jp_fingerprint
    acked_servers: list[str] | None = None
    activation_candidates: list[dict] = []
    for event_id in preview_manager.tracked_event_ids():
        if preview_manager.is_official_active(event_id):
            continue
        event = await cn_ctx.md.events.find_by_id(event_id)
        if not event:
            continue
        preview_manager.mark_cn_detected(
            event_id,
            str(event.get("eventType", "")),
        )
        if not await _validate_cn_event_rules_for_cutover(
            cn_ctx,
            event,
        ):
            continue
        if acked_servers is None:
            acked_servers = await _get_official_loaded_servers(
                current_cn_fingerprint
            )
        if acked_servers:
            activation_candidates.append({
                "event_id": event_id,
                "event_type": event["eventType"],
                "cn_masterdata_version": cn_version_text,
                "cn_fingerprint": current_cn_fingerprint,
                "acked_servers": acked_servers,
            })

    # 原生探针在低优先级 builder 子进程中运行，玩家命令和主事件循环不加载 CN 大文件。
    builder_request["official_canary_candidates"] = activation_candidates
    builder_request["official_canary_fingerprint"] = current_cn_fingerprint
    refresh_fingerprint = md5(
        json.dumps(
            {
                "cn_source_fingerprint": current_cn_fingerprint,
                "jp_source_fingerprint": current_jp_fingerprint,
                "cn_masterdata_version": cn_version_text,
                "jp_masterdata_version": jp_version_text,
                "musicmetas_update_ts": builder_request[
                    "musicmetas_update_ts"
                ],
                "native_package_build_id": builder_request[
                    "native_package_build_id"
                ],
                "allowed_event_types": sorted(
                    str(value)
                    for value in allowed_event_types
                ),
                "route_event_allowlist": sorted(
                    int(value)
                    for value in event_allowlist
                ),
                "official_activation_candidates": activation_candidates,
                # 正式节点与 CN 数据都就绪后，原生 canary 若遇到瞬时失败，
                # 必须定期重试；同时用五分钟时间桶避免恢复成每 30 秒重建。
                "official_canary_retry_bucket": (
                    int(time.time() // PREVIEW_CUTOVER_RETRY_SECONDS)
                    if activation_candidates
                    else None
                ),
                "builder_owner": builder_owner,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {
        "builder_request": builder_request,
        "official_activation_candidates": activation_candidates,
        "refresh_fingerprint": refresh_fingerprint,
    }


preview_manager.configure(provide_preview_builder_source)


@repeat_with_interval(DECKREC_DATA_UPDATE_INTERVAL_CFG, "组卡数据更新", logger)
async def deckrec_update_data():
    for region in ALL_SERVER_REGIONS:
        try:
            ctx = SekaiHandlerContext.from_region(region)

            current_masterdata_version = await ctx.md.get_version()
            masterdata_paths = await get_deckrec_masterdata_paths(ctx)
            masterdata_fingerprint = calc_deckrec_masterdata_fingerprint(masterdata_paths)
            current_musicmetas_update_ts = await musicmetas_json.get_update_time()
            logger.debug(
                f"组卡 {region} 当前 masterdata 版本: {current_masterdata_version} "
                f"指纹: {masterdata_fingerprint[:8]} "
                f"musicmetas 更新时间: {current_musicmetas_update_ts}"
            )

            async def construct_payload(with_masterdata: bool, with_musicmetas: bool) -> bytes:
                payloads = []

                data = { 
                    'region': ctx.region,
                    'masterdata_version': str(current_masterdata_version),
                    'masterdata_fingerprint': masterdata_fingerprint,
                    'musicmetas_update_ts': int(current_musicmetas_update_ts.timestamp()),
                }
                add_payload_segment(payloads, dumps_json(data, indent=False).encode('utf-8'))

                if with_masterdata:
                    logger.info(f"为自动组卡加载 {ctx.region} masterdata")
                    for path in masterdata_paths:
                        with open(path, 'rb') as f:
                            add_payload_segment(payloads, os.path.basename(path).encode('utf-8'))
                            add_payload_segment(payloads, f.read())

                if with_musicmetas:
                    logger.info(f"为自动组卡加载 {ctx.region} musicmetas")
                    musicmetas = await musicmetas_json.get()
                    musicmetas = add_omakase_music(musicmetas)
                    add_payload_segment(payloads, b'musicmetas')
                    add_payload_segment(payloads, dumps_json(musicmetas, indent=False).encode('utf-8'))
                
                return build_multiparts_payload(payloads)

            async def req(url :str, with_masterdata: bool, with_musicmetas: bool):
                async with get_client_session().post(url + "/update_data", data=await construct_payload(with_masterdata, with_musicmetas)) as resp:
                    if not (with_masterdata or with_musicmetas) and resp.status == 426:
                        data = await resp.json()
                        missing_data = data.get('detail', {}).get('missing_data', [])
                        if not missing_data:
                            logger.warning(f"{region} 组卡数据需要更新但未指明具体内容")
                            return
                        logger.info(f"{region} 组卡数据需要更新: {missing_data}")
                        await req(
                            url,
                            with_masterdata = 'masterdata' in missing_data,
                            with_musicmetas = 'musicmetas' in missing_data,
                        )
                        logger.info(f"{region} 组卡数据更新完成")
                        return
                    elif resp.status != 200:
                        msg = f"更新 {url} 组卡数据失败 ({resp.status}): "
                        try:
                            err_data = await resp.json()
                            msg += err_data.get('detail', '')
                        except:
                            try:
                                msg += await resp.text()
                            except:
                                pass
                        raise Exception(msg)

            for server in RECOMMEND_SERVERS_CFG.get():
                await req(server['url'], False, False)

        except Exception as e:
            logger.warning(f"更新组卡数据失败 ({region}): {get_exc_desc(e)}")

    # Preview 初始化是去重后台任务，不等待 builder 或旁路节点，不阻塞正式同步周期。
    preview_manager.wake()
