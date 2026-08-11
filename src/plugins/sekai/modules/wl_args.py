import os
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Iterable, Optional, Tuple


MAX_WL_TURN = 99
DEFAULT_WL_SIMULATION_MAX_TURN = 2


@dataclass(frozen=True)
class WlTurnSelector:
    """“第几次 WL 活动”选择结果，不承载章节含义。"""

    turn: int
    matched_text: str
    force_simulated: bool
    legacy: bool


# ================================ 文本归一化 ================================ #

def normalize_wl_args(text: str) -> str:
    """压缩参数中的多余空白，便于后续精确移除已匹配片段。"""

    return re.sub(r"\s+", " ", text).strip()


def remove_matched_text(text: str, matched_text: str) -> str:
    """只移除一次已确认的选择器，避免旧逻辑全局 replace('wl') 误伤其他参数。"""

    if not matched_text:
        return normalize_wl_args(text)
    return normalize_wl_args(text.replace(matched_text, " ", 1))


# ================================ WL 活动轮次 ================================ #

def get_wl_simulation_max_turn() -> int:
    """读取启动期原生能力探测结果；异常配置回退到原版仅支持的 WL2。"""

    raw_value = os.getenv(
        "DECKREC_WL_SIMULATION_MAX_TURN",
        str(DEFAULT_WL_SIMULATION_MAX_TURN),
    )
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        return DEFAULT_WL_SIMULATION_MAX_TURN
    return min(MAX_WL_TURN, max(DEFAULT_WL_SIMULATION_MAX_TURN, value))


def extract_wl_turn_selector(
    text: str,
    *,
    reserved_arguments: Iterable[str] = (),
) -> Optional[WlTurnSelector]:
    """
    提取“第几次 WL 活动”。

    `wlN` 仅作为第几次 WL 的短写，绝不再解释为章节号。
    `模拟WLN` 明确要求走模拟活动；轮次限制为 1～99，防止异常超长输入。
    调用方可保留数字开头的既有参数，例如活动筛选中的 `wl 25h`。
    """

    reserved_spans = [
        match.span()
        for argument in reserved_arguments
        for match in re.finditer(
            rf"(?i)(?<![a-z0-9])wl\s*{re.escape(argument)}(?![a-z0-9])",
            text,
        )
    ]

    patterns = (
        (r"(?i)(?:模拟|sim)\s*wl\s*([1-9]\d?)(?!\d)", True, False),
        (r"(?i)第\s*([1-9]\d?)\s*(?:次|轮)\s*wl(?!\d)", False, False),
        # 原版允许 `wl2rin` 这类紧凑写法；这里只放宽数字后的边界，
        # 仍要求 `wl` 前面不是字母或数字，避免在普通单词内部误命中。
        (r"(?i)(?<![a-z0-9])wl\s*([1-9]\d?)(?!\d)", False, True),
    )
    for pattern, force_simulated, legacy in patterns:
        for match in re.finditer(pattern, text):
            if any(
                match.start() < reserved_end and match.end() > reserved_start
                for reserved_start, reserved_end in reserved_spans
            ):
                continue
            return WlTurnSelector(
                turn=int(match.group(1)),
                matched_text=match.group(0),
                force_simulated=force_simulated,
                legacy=legacy,
            )
    return None


# ================================ WL轮次索引 ================================ #

def build_world_bloom_turn_index(
    events: Iterable[dict[str, Any]],
    chapters: Iterable[dict[str, Any]],
) -> dict[int, tuple[dict[str, Any], ...]]:
    """按角色整理真实 WL 活动时间线，作为所有 WL 轮次语义的唯一数据源。"""

    event_by_id = {
        event.get("id"): event
        for event in events
        if event.get("eventType") == "world_bloom"
        and isinstance(event.get("id"), int)
    }
    event_ids_by_character: dict[int, set[int]] = defaultdict(set)
    for chapter in chapters:
        character_id = chapter.get("gameCharacterId")
        event_id = chapter.get("eventId")
        if (
            isinstance(character_id, int)
            and character_id > 0
            and event_id in event_by_id
        ):
            event_ids_by_character[character_id].add(event_id)

    return {
        character_id: tuple(sorted(
            (event_by_id[event_id] for event_id in event_ids),
            key=lambda event: (
                event.get("startAt", 10**18),
                event.get("id", 10**9),
            ),
        ))
        for character_id, event_ids in event_ids_by_character.items()
    }


def select_world_bloom_turn(
    events: Iterable[dict[str, Any]],
    chapters: Iterable[dict[str, Any]],
    *,
    character_id: int,
    turn: int,
) -> dict[str, Any]:
    """按真实章节数据定位某个角色的第 N 次 WL 活动。"""

    if turn <= 0:
        raise ValueError("WL 轮次必须大于 0")
    candidates = build_world_bloom_turn_index(events, chapters).get(
        character_id,
        (),
    )
    if turn > len(candidates):
        raise ValueError(f"找不到角色 {character_id} 的第{turn}次真实 WL 活动")
    return candidates[turn - 1]


def collect_world_bloom_turn_events(
    events: Iterable[dict[str, Any]],
    chapters: Iterable[dict[str, Any]],
    *,
    turn: int,
) -> list[dict[str, Any]]:
    """汇总所有角色的第 N 次 WL，并按活动去重后返回活动列表。"""

    if turn <= 0:
        raise ValueError("WL 轮次必须大于 0")
    selected_by_id: dict[int, dict[str, Any]] = {}
    for candidates in build_world_bloom_turn_index(events, chapters).values():
        if turn <= len(candidates):
            event = candidates[turn - 1]
            selected_by_id[event["id"]] = event
    return sorted(
        selected_by_id.values(),
        key=lambda event: (
            event.get("startAt", 10**18),
            event.get("id", 10**9),
        ),
    )


# ================================ WL 章节选择 ================================ #

def extract_wl_chapter_selector(text: str) -> Tuple[Optional[int], Optional[str]]:
    """
    提取显式章节参数。

    独立的 `wl2` 仍表示第二次 WL；仅保留原版已经支持的紧贴活动号写法
    `event179wl1` / `活动179wl1`，将其中的 `wl1` 解释为该活动第一章。
    """

    match = re.search(r"(?i)(?:章节|chapter|ch)\s*(\d+)", text)
    if match:
        return int(match.group(1)), match.group(0)

    legacy_match = re.search(
        r"(?i)(?:活动|event)\s*\d+"
        r"(?P<selector>wl\s*(?P<chapter>[1-9])(?!\d))",
        text,
    )
    if legacy_match:
        return (
            int(legacy_match.group("chapter")),
            legacy_match.group("selector"),
        )
    return None, None


def extract_wl_role_selector(
    text: str,
    nicknames: Iterable[str],
    *,
    allow_bare: bool = False,
) -> Tuple[Optional[str], Optional[str]]:
    """
    提取角色章节参数。

    新语法使用“角色miku”；实时榜线带独立 `wl` 前缀时可允许旧的裸昵称写法。
    """

    ordered_nicknames = sorted(set(nicknames), key=len, reverse=True)
    for nickname in ordered_nicknames:
        escaped = re.escape(nickname)
        match = re.search(rf"(?i)(?:角色|role)\s*{escaped}(?![a-z0-9])", text)
        if match:
            return nickname, match.group(0)

    if allow_bare:
        for nickname in ordered_nicknames:
            escaped = re.escape(nickname)
            match = re.search(rf"(?i)(?<![a-z0-9]){escaped}(?![a-z0-9])", text)
            if match:
                return nickname, match.group(0)

        # 原生解析曾允许 `event179rin` / `活动179rin`。只在明确的活动号或
        # WL 轮次后恢复该行为，不把普通单词中的昵称子串当作章节参数。
        for nickname in ordered_nicknames:
            escaped = re.escape(nickname)
            match = re.search(
                rf"(?i)(?:(?:活动|event)\s*\d+|wl\s*[1-9]\d?)"
                rf"(?P<nickname>{escaped})",
                text,
            )
            if match:
                return nickname, match.group("nickname")
    return None, None


def remove_standalone_wl(text: str) -> Tuple[bool, str]:
    """移除一个独立的 `wl` 当前章节标记，返回是否匹配及剩余参数。"""

    match = re.search(r"(?i)(?<![a-z0-9])wl(?![a-z0-9])", text)
    if not match:
        return False, normalize_wl_args(text)
    return True, normalize_wl_args(text[:match.start()] + " " + text[match.end():])
