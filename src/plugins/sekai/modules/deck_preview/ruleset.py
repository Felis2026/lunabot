from __future__ import annotations

import hashlib
import importlib.metadata
import json
import math
import os
import shutil
import time
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from src.services.deck_recommender.masterdata_spec import (
    get_native_package_build_id,
    get_preview_manifest_fingerprint_input,
    get_spec_filenames,
)

from .protocol import PREVIEW_DATA_SCOPE, PREVIEW_NATIVE_REGION


MANIFEST_SCHEMA_VERSION = 1
BUILDER_VERSION = 2
SUPPORTED_EVENT_TYPES = frozenset({"marathon", "world_bloom"})
JP_GLOBAL_OVERRIDE_FILE = "worldBloomSupportDeckBonuses.json"


class RulesetBuildError(RuntimeError):
    """规则集来源、闭包或发布过程不满足安全约束。"""


# ================================ 正式切回闭包校验 ================================ #

def _valid_cutover_rate(value: Any) -> bool:
    """检查正式 CN WL 支援倍率是否为可用的非负有限数值。"""

    return (
        isinstance(value, (int, float))
        and math.isfinite(value)
        and value >= 0
    )


def validate_event_card_closure(
    event_cards: Iterable[Mapping[str, Any]],
    cards: Iterable[Mapping[str, Any]],
    skills: Iterable[Mapping[str, Any]],
    episode_card_ids: Iterable[int],
) -> bool:
    """确认活动卡、卡牌、技能和剧情均已随 CN MasterData 完整落地。"""

    event_card_rows = list(event_cards)
    if (
        not event_card_rows
        or any(not isinstance(row, Mapping) for row in event_card_rows)
    ):
        return False
    cards_by_id = {
        card["id"]: card
        for card in cards
        if isinstance(card.get("id"), int)
    }
    skill_ids = {
        skill["id"]
        for skill in skills
        if isinstance(skill.get("id"), int)
    }
    episode_ids = {int(card_id) for card_id in episode_card_ids}

    for event_card in event_card_rows:
        card_id = event_card.get("cardId")
        card = cards_by_id.get(card_id)
        if (
            card is None
            or card_id not in episode_ids
            or card.get("skillId") not in skill_ids
            or not isinstance(card.get("characterId"), int)
            or not card.get("cardRarityType")
            or not card.get("attr")
        ):
            return False
    return True


def validate_world_bloom_support_tables(
    support_bonus_rows: Iterable[Mapping[str, Any]],
    different_attr_rows: Iterable[Mapping[str, Any]],
) -> bool:
    """验证 WL3 支援队依赖的全局倍率表及其三组嵌套倍率。"""

    support_rows = list(support_bonus_rows)
    attribute_rows = list(different_attr_rows)
    if (
        not support_rows
        or not attribute_rows
        or any(not isinstance(row, Mapping) for row in support_rows)
        or any(not isinstance(row, Mapping) for row in attribute_rows)
    ):
        return False

    expected_rarities = {
        "rarity_1",
        "rarity_2",
        "rarity_3",
        "rarity_4",
        "rarity_birthday",
    }
    if not expected_rarities.issubset({
        str(row.get("cardRarityType"))
        for row in support_rows
    }):
        return False

    required_nested_bonus_keys = (
        "worldBloomSupportDeckCharacterBonuses",
        "worldBloomSupportDeckMasterRankBonuses",
        "worldBloomSupportDeckSkillLevelBonuses",
    )
    for row in support_rows:
        if not row.get("cardRarityType"):
            return False
        for key in required_nested_bonus_keys:
            nested_rows = row.get(key)
            if not isinstance(nested_rows, list) or not nested_rows:
                return False
            if any(
                not isinstance(item, Mapping)
                or not _valid_cutover_rate(item.get("bonusRate"))
                for item in nested_rows
            ):
                return False
        if {
            item.get("worldBloomSupportDeckCharacterType")
            for item in row["worldBloomSupportDeckCharacterBonuses"]
        } != {"specific", "others"}:
            return False
        if {
            item.get("masterRank")
            for item in row["worldBloomSupportDeckMasterRankBonuses"]
        } != set(range(6)):
            return False
        if {
            item.get("skillLevel")
            for item in row["worldBloomSupportDeckSkillLevelBonuses"]
        } != set(range(1, 5)):
            return False

    return {item.get("attributeCount") for item in attribute_rows} == set(
        range(1, 6)
    ) and all(
        isinstance(item.get("attributeCount"), int)
        and _valid_cutover_rate(item.get("bonusRate"))
        for item in attribute_rows
    )


@dataclass(frozen=True)
class FrozenSourceSnapshot:
    """一次构建使用的完整内存快照，避免同一文件在合并期间被二次读取。"""

    contents: dict[str, bytes]
    hashes: dict[str, str]
    paths: dict[str, str]


@dataclass(frozen=True)
class RulesetBuildResult:
    """子进程返回给调度器的发布结果。"""

    scope_fingerprint: str
    version_dir: str
    manifest_path: str
    event_ids: tuple[int, ...]
    future_card_ids: tuple[int, ...]
    reused: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope_fingerprint": self.scope_fingerprint,
            "version_dir": self.version_dir,
            "manifest_path": self.manifest_path,
            "event_ids": list(self.event_ids),
            "future_card_ids": list(self.future_card_ids),
            "reused": self.reused,
        }


# ================================ 文件与哈希工具 ================================ #

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


def _load_json_list(data: bytes, source_name: str) -> list[dict[str, Any]]:
    try:
        value = json.loads(data)
    except Exception as exc:
        raise RulesetBuildError(f"{source_name} 不是合法 JSON: {exc}") from exc
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise RulesetBuildError(f"{source_name} 必须是对象数组")
    return value


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temp_path.open("wb") as file:
        file.write(data)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temp_path, path)


def _safe_remove_tree(path: Path, allowed_parent: Path) -> None:
    resolved = path.resolve()
    parent = allowed_parent.resolve()
    if resolved == parent or parent not in resolved.parents:
        raise RulesetBuildError(f"拒绝清理规则集范围外路径: {resolved}")
    if resolved.exists():
        shutil.rmtree(resolved)


def _native_package_version() -> str:
    try:
        return importlib.metadata.version("sekai-deck-recommend-cpp")
    except importlib.metadata.PackageNotFoundError:
        return "missing"


def freeze_sources(paths: Mapping[str, str]) -> FrozenSourceSnapshot:
    """完整读取并哈希所有来源文件；key 使用 `cn/name.json` 这类稳定标识。"""

    contents: dict[str, bytes] = {}
    hashes: dict[str, str] = {}
    normalized_paths: dict[str, str] = {}
    for key in sorted(paths):
        path = Path(paths[key]).resolve()
        if not path.is_file():
            raise RulesetBuildError(f"来源文件不存在: {key}")
        data = path.read_bytes()
        _load_json_list(data, key)
        contents[key] = data
        hashes[key] = _sha256_bytes(data)
        normalized_paths[key] = str(path)
    return FrozenSourceSnapshot(contents, hashes, normalized_paths)


def verify_sources_unchanged(snapshot: FrozenSourceSnapshot) -> None:
    """发布前复核来源内容，防止跨版本文件被拼进同一规则集。"""

    changed: list[str] = []
    for key, path_text in snapshot.paths.items():
        path = Path(path_text)
        if not path.is_file() or _sha256_bytes(path.read_bytes()) != snapshot.hashes[key]:
            changed.append(key)
    if changed:
        raise RulesetBuildError(f"构建期间来源文件发生变化: {', '.join(changed)}")


# ================================ 合并与闭包校验 ================================ #

def _ids(items: Iterable[dict[str, Any]], key: str = "id") -> set[int]:
    return {
        int(item[key])
        for item in items
        if isinstance(item.get(key), int)
    }


def _ensure_unique_ids(items: Sequence[dict[str, Any]], name: str) -> None:
    ids = [item.get("id") for item in items]
    if any(not isinstance(item_id, int) for item_id in ids):
        raise RulesetBuildError(f"{name} 存在非整数主键")
    if len(ids) != len(set(ids)):
        raise RulesetBuildError(f"{name} 存在重复主键")


def _append_without_id_conflict(
    base: list[dict[str, Any]],
    additions: Iterable[dict[str, Any]],
    name: str,
) -> list[dict[str, Any]]:
    result = list(base)
    existing = _ids(base)
    for item in additions:
        item_id = item.get("id")
        if not isinstance(item_id, int):
            raise RulesetBuildError(f"{name} 新增行缺少整数主键")
        if item_id in existing:
            raise RulesetBuildError(f"{name} 新增主键与 CN 冲突: {item_id}")
        existing.add(item_id)
        result.append(item)
    _ensure_unique_ids(result, name)
    return result


def _append_without_composite_conflict(
    base: list[dict[str, Any]],
    additions: Iterable[dict[str, Any]],
    name: str,
    key_fields: Sequence[str],
    *,
    strip_fields: Sequence[str] = (),
) -> list[dict[str, Any]]:
    """兼容 compact MasterData 中省略 `id`、改用业务复合键的表。"""

    result = list(base)

    def item_key(item: Mapping[str, Any]) -> tuple[Any, ...]:
        key = tuple(item.get(field) for field in key_fields)
        if any(value is None for value in key):
            raise RulesetBuildError(f"{name} 缺少复合主键字段 {key_fields}")
        return key

    existing = {item_key(item) for item in base}
    if len(existing) != len(base):
        raise RulesetBuildError(f"{name} 的 CN 基础表存在重复复合主键")
    for raw_item in additions:
        item = {
            key: value
            for key, value in raw_item.items()
            if key not in strip_fields
        }
        key = item_key(item)
        if key in existing:
            raise RulesetBuildError(f"{name} 新增复合主键与 CN 冲突: {key}")
        existing.add(key)
        result.append(item)
    return result


def _validate_rate(value: Any, label: str) -> None:
    if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise RulesetBuildError(f"{label} 必须是有限非负数")


def _candidate_events(
    cn_events: list[dict[str, Any]],
    jp_events: list[dict[str, Any]],
    allowed_event_types: set[str],
    event_allowlist: set[int],
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    cn_ids = _ids(cn_events)
    cn_max = max(cn_ids, default=0)
    accepted: list[dict[str, Any]] = []
    excluded: dict[str, str] = {}

    for event in sorted(jp_events, key=lambda item: item.get("id", 0)):
        event_id = event.get("id")
        if not isinstance(event_id, int) or event_id in cn_ids or event_id <= cn_max:
            continue
        if event_allowlist and event_id not in event_allowlist:
            excluded[str(event_id)] = "not_in_event_allowlist"
            continue
        event_type = event.get("eventType")
        if event_type not in allowed_event_types:
            excluded[str(event_id)] = f"unsupported_event_type: {event_type}"
            continue
        if not isinstance(event.get("assetbundleName"), str) or not event["assetbundleName"]:
            excluded[str(event_id)] = "missing_assetbundle_name"
            continue
        if not all(isinstance(event.get(key), int) for key in ("startAt", "aggregateAt")):
            excluded[str(event_id)] = "invalid_event_time"
            continue
        accepted.append(event)
    return accepted, excluded


def _validate_future_cards(
    cn: Mapping[str, list[dict[str, Any]]],
    jp: Mapping[str, list[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    cn_card_ids = _ids(cn["cards.json"])
    future_cards = [card for card in jp["cards.json"] if card.get("id") not in cn_card_ids]
    future_card_ids = _ids(future_cards)
    if len(future_cards) != len(future_card_ids):
        raise RulesetBuildError("JP 未实装卡目录存在重复或非法卡牌 ID")

    cn_skill_ids = _ids(cn["skills.json"])
    cn_character_ids = _ids(cn["gameCharacters.json"])
    cn_rarities = {
        item.get("cardRarityType")
        for item in cn["cardRarities.json"]
    }
    valid_attrs = {
        item.get("attr")
        for item in cn["cards.json"]
        if isinstance(item.get("attr"), str)
    }
    for card in future_cards:
        card_id = card["id"]
        if card.get("skillId") not in cn_skill_ids:
            raise RulesetBuildError(f"未来卡 {card_id} 的 skillId 无法由 CN 解析")
        if card.get("characterId") not in cn_character_ids:
            raise RulesetBuildError(f"未来卡 {card_id} 的 characterId 无法由 CN 解析")
        if card.get("cardRarityType") not in cn_rarities:
            raise RulesetBuildError(f"未来卡 {card_id} 的 rarity 无法由 CN 解析")
        if card.get("attr") not in valid_attrs:
            raise RulesetBuildError(f"未来卡 {card_id} 的属性无法由 CN 解析")

    cn_episode_ids = _ids(cn["cardEpisodes.json"])
    future_episodes = [
        episode
        for episode in jp["cardEpisodes.json"]
        if episode.get("cardId") in future_card_ids
    ]
    episode_ids = _ids(future_episodes)
    if len(future_episodes) != len(episode_ids):
        raise RulesetBuildError("未来卡剧情目录存在重复或非法主键")
    if episode_ids & cn_episode_ids:
        raise RulesetBuildError("未来卡剧情主键与 CN 冲突")

    episodes_by_card: dict[int, list[dict[str, Any]]] = {}
    for episode in future_episodes:
        episodes_by_card.setdefault(episode["cardId"], []).append(episode)
    missing = sorted(card_id for card_id in future_card_ids if not episodes_by_card.get(card_id))
    if missing:
        raise RulesetBuildError(f"未来卡缺少剧情闭包: {missing[:10]}")
    return future_cards, future_episodes


def _validate_event_rules(
    candidate: dict[str, Any],
    cn: Mapping[str, list[dict[str, Any]]],
    jp: Mapping[str, list[dict[str, Any]]],
    all_card_ids: set[int],
) -> None:
    event_id = candidate["id"]
    bonuses = [
        item for item in jp["eventDeckBonuses.json"]
        if item.get("eventId") == event_id
    ]
    if not bonuses:
        raise RulesetBuildError(f"活动 {event_id} 缺少 eventDeckBonuses")

    unit_ids = _ids(cn["gameCharacterUnits.json"])
    valid_attrs = {
        item.get("attr")
        for item in cn["cards.json"]
        if isinstance(item.get("attr"), str)
    }
    for bonus in bonuses:
        # `None` 与 cardAttr 的 `None` 对称，分别表示“只限定属性/只限定角色”。
        if (
            bonus.get("gameCharacterUnitId") is not None
            and bonus.get("gameCharacterUnitId") not in unit_ids
        ):
            raise RulesetBuildError(
                f"活动 {event_id} 加成引用未知 gameCharacterUnitId"
            )
        # `None` 是官方“仅角色/团加成、不限定属性”的合法语义。
        if bonus.get("cardAttr") is not None and bonus.get("cardAttr") not in valid_attrs:
            raise RulesetBuildError(f"活动 {event_id} 加成引用未知属性")
        _validate_rate(bonus.get("bonusRate"), f"活动 {event_id} bonusRate")

    event_cards = [
        item for item in jp["eventCards.json"]
        if item.get("eventId") == event_id
    ]
    for event_card in event_cards:
        if event_card.get("cardId") not in all_card_ids:
            raise RulesetBuildError(
                f"活动 {event_id} 的 eventCards 悬空: {event_card.get('cardId')}"
            )

    if candidate["eventType"] != "world_bloom":
        return

    chapters = [
        item for item in jp["worldBlooms.json"]
        if item.get("eventId") == event_id
    ]
    if not chapters:
        raise RulesetBuildError(f"WL {event_id} 缺少章节")
    chapter_keys: set[tuple[int, int]] = set()
    character_ids = _ids(cn["gameCharacters.json"])
    for chapter in chapters:
        chapter_key = (chapter.get("chapterNo"), chapter.get("gameCharacterId"))
        if (
            not all(isinstance(value, int) and value > 0 for value in chapter_key)
            or chapter_key in chapter_keys
        ):
            raise RulesetBuildError(f"WL {event_id} 章节号或角色不唯一")
        if chapter["gameCharacterId"] not in character_ids:
            raise RulesetBuildError(f"WL {event_id} 章节引用未知角色")
        chapter_keys.add(chapter_key)

    limited = [
        item
        for item in jp["worldBloomSupportDeckUnitEventLimitedBonuses.json"]
        if item.get("eventId") == event_id
    ]
    for bonus in limited:
        if bonus.get("cardId") not in all_card_ids:
            raise RulesetBuildError(f"WL {event_id} 限定支援引用未知卡牌")
        if bonus.get("gameCharacterId") not in character_ids:
            raise RulesetBuildError(f"WL {event_id} 限定支援引用未知角色")
        _validate_rate(bonus.get("bonusRate"), f"WL {event_id} 支援 bonusRate")

    if not any(
        item.get("eventId") == event_id
        for item in jp["eventExchangeSummaries.json"]
    ):
        raise RulesetBuildError(f"WL {event_id} 缺少 eventExchangeSummaries")
    if not any(item.get("eventId") == event_id for item in jp["eventItems.json"]):
        raise RulesetBuildError(f"WL {event_id} 缺少 eventItems")


def build_ruleset_contents(
    snapshot: FrozenSourceSnapshot,
    expected_filenames: Sequence[str],
    *,
    allowed_event_types: Iterable[str] = SUPPORTED_EVENT_TYPES,
    event_allowlist: Iterable[int] = (),
) -> tuple[dict[str, bytes], dict[str, Any]]:
    """从冻结的 CN/JP 来源生成完整 preview 文件集和未签名 manifest。"""

    expected = tuple(expected_filenames)
    if len(expected) != len(set(expected)):
        raise RulesetBuildError("预期 MasterData 文件集合存在重复")
    expected_set = set(expected)
    if JP_GLOBAL_OVERRIDE_FILE not in expected_set:
        raise RulesetBuildError("共享文件规格缺少 WL 全局支援倍率表")

    cn: dict[str, list[dict[str, Any]]] = {}
    jp: dict[str, list[dict[str, Any]]] = {}
    for filename in expected:
        for region, target in (("cn", cn), ("jp", jp)):
            key = f"{region}/{filename}"
            if key not in snapshot.contents:
                raise RulesetBuildError(f"来源快照缺少 {key}")
            target[filename] = _load_json_list(snapshot.contents[key], key)

    allowed_types = set(allowed_event_types)
    if not allowed_types or not allowed_types <= SUPPORTED_EVENT_TYPES:
        raise RulesetBuildError("allowed_event_types 只能收窄 marathon/world_bloom")
    candidates, excluded = _candidate_events(
        cn["events.json"],
        jp["events.json"],
        allowed_types,
        {int(value) for value in event_allowlist},
    )
    future_cards, future_episodes = _validate_future_cards(cn, jp)
    all_card_ids = _ids(cn["cards.json"]) | _ids(future_cards)

    accepted: list[dict[str, Any]] = []
    for candidate in candidates:
        try:
            _validate_event_rules(candidate, cn, jp, all_card_ids)
            accepted.append(candidate)
        except RulesetBuildError as exc:
            excluded[str(candidate["id"])] = str(exc)

    accepted_ids = {event["id"] for event in accepted}
    wl_ids = {
        event["id"]
        for event in accepted
        if event["eventType"] == "world_bloom"
    }

    # ================================ 规则表合并 ================================ #
    outputs: dict[str, bytes] = {
        filename: snapshot.contents[f"cn/{filename}"]
        for filename in expected
    }

    changed: dict[str, list[dict[str, Any]]] = {}
    changed["events.json"] = _append_without_id_conflict(
        cn["events.json"],
        accepted,
        "events",
    )
    changed["eventDeckBonuses.json"] = _append_without_id_conflict(
        cn["eventDeckBonuses.json"],
        (
            item
            for item in jp["eventDeckBonuses.json"]
            if item.get("eventId") in accepted_ids
        ),
        "eventDeckBonuses",
    )
    changed["eventCards.json"] = _append_without_composite_conflict(
        cn["eventCards.json"],
        (
            item
            for item in jp["eventCards.json"]
            if item.get("eventId") in accepted_ids
        ),
        "eventCards",
        ("eventId", "cardId"),
        # CN compact 表没有这个内部流水号；保留它会产生同一文件混合 schema。
        strip_fields=("id",),
    )
    changed["cards.json"] = _append_without_id_conflict(
        cn["cards.json"],
        future_cards,
        "cards",
    )
    changed["cardEpisodes.json"] = _append_without_id_conflict(
        cn["cardEpisodes.json"],
        future_episodes,
        "cardEpisodes",
    )

    if wl_ids:
        for filename in (
            "worldBlooms.json",
            "worldBloomSupportDeckUnitEventLimitedBonuses.json",
            "eventExchangeSummaries.json",
            "eventItems.json",
        ):
            changed[filename] = _append_without_id_conflict(
                cn[filename],
                (
                    item
                    for item in jp[filename]
                    if item.get("eventId") in wl_ids
                ),
                filename.removesuffix(".json"),
            )
        outputs[JP_GLOBAL_OVERRIDE_FILE] = snapshot.contents[
            f"jp/{JP_GLOBAL_OVERRIDE_FILE}"
        ]

    for filename, value in changed.items():
        outputs[filename] = _canonical_json_bytes(value)

    if set(outputs) != expected_set:
        raise RulesetBuildError("规则集输出文件与共享规格不一致")

    file_hashes = {
        filename: _sha256_bytes(outputs[filename])
        for filename in sorted(outputs)
    }
    future_card_ids = sorted(_ids(future_cards))
    unsigned_manifest: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "data_scope": PREVIEW_DATA_SCOPE,
        "native_region": PREVIEW_NATIVE_REGION,
        "event_ids": sorted(accepted_ids),
        "event_types": {
            str(event["id"]): event["eventType"]
            for event in accepted
        },
        "world_bloom_chapters": {
            str(event_id): [
                {
                    "chapter_no": item["chapterNo"],
                    "game_character_id": item["gameCharacterId"],
                }
                for item in sorted(
                    (
                        chapter
                        for chapter in jp["worldBlooms.json"]
                        if chapter.get("eventId") == event_id
                    ),
                    key=lambda chapter: (
                        chapter["chapterNo"],
                        chapter["gameCharacterId"],
                    ),
                )
            ]
            for event_id in sorted(wl_ids)
        },
        "global_overrides": (
            {JP_GLOBAL_OVERRIDE_FILE: "jp"}
            if wl_ids
            else {}
        ),
        "future_cards": {
            "ids": future_card_ids,
            "count": len(future_card_ids),
            "sha256": _sha256_bytes(_canonical_json_bytes(future_card_ids)),
        },
        "excluded_events": excluded,
        "files": file_hashes,
        "source_files": dict(sorted(snapshot.hashes.items())),
        "builder_version": BUILDER_VERSION,
        "native_package_version": _native_package_version(),
        # 上游与 StarMoe fork 当前都报告 0.2.21，必须额外区分实际二进制。
        "native_package_build_id": get_native_package_build_id(),
    }
    return outputs, unsigned_manifest


# ================================ 原生冷加载与计算探针 ================================ #

def _build_canary_profile(
    cn_cards: Sequence[dict[str, Any]],
    assumed_card: dict[str, Any] | None = None,
) -> dict[str, Any]:
    user_cards = [
        {
            "cardId": card["id"],
            "level": 1,
            "skillLevel": 1,
            "masterRank": 0,
            "specialTrainingStatus": "none",
            "defaultImage": "original",
            "episodes": [],
        }
        for card in cn_cards
    ]
    if assumed_card is not None:
        user_cards.append({
            "cardId": assumed_card["id"],
            "level": 1,
            "skillLevel": 1,
            "masterRank": 0,
            "specialTrainingStatus": "none",
            "defaultImage": "original",
            "episodes": [],
        })
    return {
        "userGamedata": {},
        "userDecks": [],
        "userCards": user_cards,
        "userHonors": [],
        "userMysekaiCanvases": [],
        "userCharacters": [
            {"characterId": character_id, "characterRank": 1}
            for character_id in range(1, 27)
        ],
        "userMysekaiGates": [],
        "userMysekaiFixtureGameCharacterPerformanceBonuses": [],
        "userAreas": [],
    }


def run_native_canaries(
    masterdata_dir: Path,
    musicmetas_path: str,
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    """冷加载合成数据并逐活动/逐 WL 章节执行固定主队原生计算。"""

    try:
        from sekai_deck_recommend_cpp import (
            DeckRecommendOptions,
            DeckRecommendUserData,
            SekaiDeckRecommend,
        )
    except ImportError as exc:
        raise RulesetBuildError("未安装 sekai-deck-recommend-cpp，无法执行 canary") from exc

    music_path = Path(musicmetas_path).resolve()
    if not music_path.is_file():
        raise RulesetBuildError("原生 canary 缺少 MusicMetas 文件")

    recommender = SekaiDeckRecommend()
    recommender.update_masterdata(str(masterdata_dir), PREVIEW_NATIVE_REGION)
    recommender.update_musicmetas(str(music_path), PREVIEW_NATIVE_REGION)

    cn_cards = _load_json_list(
        (masterdata_dir / "cards.json").read_bytes(),
        "output/cards.json",
    )
    future_ids = set(manifest["future_cards"]["ids"])
    cn_cards = [card for card in cn_cards if card["id"] not in future_ids]
    cn_card_ids = _ids(cn_cards)
    preferred_fixed = [1, 5, 9, 13, 17]
    fixed_cards = [card_id for card_id in preferred_fixed if card_id in cn_card_ids]
    if len(fixed_cards) < 5:
        fixed_cards = sorted(cn_card_ids)[:5]
    if len(fixed_cards) != 5:
        raise RulesetBuildError("原生 canary 找不到五张 CN 基础卡")

    def load_userdata(profile: dict[str, Any]):
        userdata = DeckRecommendUserData()
        userdata.load_from_bytes(_canonical_json_bytes(profile))
        return userdata

    cn_userdata = load_userdata(_build_canary_profile(cn_cards))

    def recommend(
        event_id: int,
        wl_character_id: int | None,
        *,
        userdata,
        fixed: Sequence[int],
    ):
        options = DeckRecommendOptions()
        options.region = PREVIEW_NATIVE_REGION
        options.algorithm = "dfs"
        options.live_type = "solo"
        options.music_id = 74
        options.music_diff = "expert"
        options.event_id = event_id
        options.world_bloom_character_id = wl_character_id
        options.limit = 1
        options.fixed_cards = list(fixed)
        options.user_data = userdata
        options.target = "score"
        options.timeout_ms = 30_000
        result = recommender.recommend(options)
        if not result.decks:
            raise RulesetBuildError(
                f"原生 canary 未返回卡组: event={event_id} wl={wl_character_id}"
            )
        returned = {
            card.card_id
            for card in result.decks[0].cards
        }
        if not returned <= set(fixed):
            raise RulesetBuildError(
                f"原生 canary 返回未提交卡牌: event={event_id} cards={sorted(returned)}"
            )
        support_rate = result.decks[0].support_deck_bonus_rate
        if not isinstance(support_rate, (int, float)) or not math.isfinite(support_rate):
            raise RulesetBuildError(f"WL {event_id} 支援加成不是有限数")
        return result.decks[0]

    event_count = 0
    wl_chapter_count = 0
    chapters = manifest.get("world_bloom_chapters", {})
    for event_id in manifest["event_ids"]:
        event_chapters = chapters.get(str(event_id), [])
        if event_chapters:
            for chapter in event_chapters:
                recommend(
                    event_id,
                    chapter["game_character_id"],
                    userdata=cn_userdata,
                    fixed=fixed_cards,
                )
                wl_chapter_count += 1
        else:
            recommend(
                event_id,
                None,
                userdata=cn_userdata,
                fixed=fixed_cards,
            )
        event_count += 1

    # 同时探测目录首尾两张未来卡，覆盖“当期卡”和“非当期未来卡”两类假设。
    future_cards_by_id = {
        card["id"]: card
        for card in _load_json_list(
            (masterdata_dir / "cards.json").read_bytes(),
            "output/cards.json",
        )
        if card["id"] in future_ids
    }
    assumed_count = 0
    if manifest["event_ids"] and future_cards_by_id:
        canary_event_id = manifest["event_ids"][0]
        for card_id in sorted(future_cards_by_id)[:: max(len(future_cards_by_id) - 1, 1)]:
            assumed_userdata = load_userdata(
                _build_canary_profile(cn_cards, future_cards_by_id[card_id])
            )
            fixed = [card_id] + fixed_cards[:4]
            recommend(
                canary_event_id,
                None,
                userdata=assumed_userdata,
                fixed=fixed,
            )
            assumed_count += 1

    return {
        "event_count": event_count,
        "world_bloom_chapter_count": wl_chapter_count,
        "assumed_card_count": assumed_count,
        "cold_load": True,
    }


def run_official_native_canaries(
    masterdata_dir: Path,
    musicmetas_path: str,
    candidates: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """
    用纯 CN 快照逐活动执行正式切回探针。

    单个候选失败只会阻止该活动切回，不应连带使仍然可用的 preview 规则构建失败。
    """

    if not candidates:
        return {
            "confirmed_event_ids": [],
            "failures": {},
            "cold_load": False,
        }

    # ================================ 正式候选闭包 ================================ #
    events = _load_json_list(
        (masterdata_dir / "events.json").read_bytes(),
        "official/events.json",
    )
    events_by_id = {
        event["id"]: event
        for event in events
        if isinstance(event.get("id"), int)
    }
    world_blooms = _load_json_list(
        (masterdata_dir / "worldBlooms.json").read_bytes(),
        "official/worldBlooms.json",
    )

    confirmed: list[int] = []
    failures: dict[str, str] = {}
    seen: set[int] = set()

    # ================================ 逐活动原生探针 ================================ #
    for candidate in candidates:
        raw_event_id = candidate.get("event_id")
        try:
            if not isinstance(raw_event_id, int) or raw_event_id <= 0:
                raise RulesetBuildError("正式切回候选缺少正整数 event_id")
            event_id = raw_event_id
            if event_id in seen:
                raise RulesetBuildError("正式切回候选 event_id 重复")
            seen.add(event_id)

            event = events_by_id.get(event_id)
            if event is None:
                raise RulesetBuildError("CN 快照中不存在该活动")
            event_type = event.get("eventType")
            if event_type != candidate.get("event_type"):
                raise RulesetBuildError("活动类型与切回候选不一致")
            if event_type not in SUPPORTED_EVENT_TYPES:
                raise RulesetBuildError(f"不支持的活动类型: {event_type}")

            chapters: list[dict[str, int]] = []
            if event_type == "world_bloom":
                chapters = [
                    {
                        "chapter_no": chapter["chapterNo"],
                        "game_character_id": chapter["gameCharacterId"],
                    }
                    for chapter in world_blooms
                    if chapter.get("eventId") == event_id
                    and isinstance(chapter.get("chapterNo"), int)
                    and isinstance(chapter.get("gameCharacterId"), int)
                ]
                if not chapters:
                    raise RulesetBuildError("CN 快照中的 WL 活动缺少章节")

            probe_manifest = {
                "event_ids": [event_id],
                "future_cards": {"ids": []},
                "world_bloom_chapters": (
                    {str(event_id): chapters}
                    if chapters
                    else {}
                ),
            }
            run_native_canaries(
                masterdata_dir,
                musicmetas_path,
                probe_manifest,
            )
        except Exception as exc:
            failure_key = (
                str(raw_event_id)
                if isinstance(raw_event_id, int)
                else f"invalid-{len(failures) + 1}"
            )
            failures[failure_key] = f"{type(exc).__name__}: {exc}"
        else:
            confirmed.append(event_id)

    return {
        "confirmed_event_ids": sorted(confirmed),
        "failures": failures,
        "cold_load": True,
    }


# ================================ staging 与原子发布 ================================ #

def _acquire_build_lock(root: Path, stale_seconds: int = 1800) -> Path:
    lock_path = root / ".build.lock"
    root.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            if time.time() - lock_path.stat().st_mtime > stale_seconds:
                lock_path.unlink()
                return _acquire_build_lock(root, stale_seconds)
        except FileNotFoundError:
            return _acquire_build_lock(root, stale_seconds)
        raise RulesetBuildError("已有 preview builder 正在运行")
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        file.write(f"pid={os.getpid()}\n")
    return lock_path


def _load_reusable_result(
    root: Path,
    source_hashes: Mapping[str, str],
    *,
    cn_masterdata_version: str,
    jp_masterdata_version: str,
    allowed_event_types: Sequence[str],
    event_allowlist: Sequence[int],
) -> RulesetBuildResult | None:
    pointer_path = root / "current.json"
    if not pointer_path.is_file():
        return None
    try:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
        version_dir = (root / pointer["version_dir"]).resolve()
        if root.resolve() not in version_dir.parents:
            return None
        manifest_path = version_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("source_files") != dict(sorted(source_hashes.items())):
            return None
        if manifest.get("cn_masterdata_version") != str(cn_masterdata_version):
            return None
        if manifest.get("jp_masterdata_version") != str(jp_masterdata_version):
            return None
        if manifest.get("builder_version") != BUILDER_VERSION:
            return None
        if manifest.get("native_package_version") != _native_package_version():
            return None
        if (
            manifest.get("native_package_build_id")
            != get_native_package_build_id()
        ):
            return None
        if manifest.get("build_policy") != {
            "allowed_event_types": list(allowed_event_types),
            "event_allowlist": list(event_allowlist),
        }:
            return None
        masterdata_dir = version_dir / "masterdata"
        for filename, expected_hash in manifest["files"].items():
            path = masterdata_dir / filename
            if not path.is_file() or _sha256_bytes(path.read_bytes()) != expected_hash:
                return None
        return RulesetBuildResult(
            scope_fingerprint=manifest["scope_fingerprint"],
            version_dir=str(version_dir),
            manifest_path=str(manifest_path),
            event_ids=tuple(manifest["event_ids"]),
            future_card_ids=tuple(manifest["future_cards"]["ids"]),
            reused=True,
        )
    except Exception:
        return None


def build_and_publish_ruleset(
    *,
    cn_paths: Mapping[str, str],
    jp_paths: Mapping[str, str],
    output_root: str,
    cn_masterdata_version: str,
    jp_masterdata_version: str,
    expected_filenames: Sequence[str],
    musicmetas_path: str,
    allowed_event_types: Iterable[str] = SUPPORTED_EVENT_TYPES,
    event_allowlist: Iterable[int] = (),
    run_canaries: bool = True,
) -> RulesetBuildResult:
    """构建、复核并原子发布一份可路由的 preview 规则集。"""

    root = Path(output_root).resolve()
    lock_path = _acquire_build_lock(root)
    staging_parent = root / "staging"
    staging_dir = staging_parent / uuid.uuid4().hex
    try:
        expected = tuple(expected_filenames)
        source_paths = {
            **{f"cn/{name}": path for name, path in cn_paths.items()},
            **{f"jp/{name}": path for name, path in jp_paths.items()},
        }
        expected_source_keys = {
            f"{region}/{filename}"
            for region in ("cn", "jp")
            for filename in expected
        }
        if set(source_paths) != expected_source_keys:
            missing = sorted(expected_source_keys - set(source_paths))
            extra = sorted(set(source_paths) - expected_source_keys)
            raise RulesetBuildError(
                f"来源文件集合不匹配 missing={missing} extra={extra}"
            )

        normalized_allowed_types = tuple(sorted(set(allowed_event_types)))
        normalized_allowlist = tuple(sorted({int(value) for value in event_allowlist}))
        snapshot = freeze_sources(source_paths)
        if reusable := _load_reusable_result(
            root,
            snapshot.hashes,
            cn_masterdata_version=str(cn_masterdata_version),
            jp_masterdata_version=str(jp_masterdata_version),
            allowed_event_types=normalized_allowed_types,
            event_allowlist=normalized_allowlist,
        ):
            return reusable

        outputs, manifest = build_ruleset_contents(
            snapshot,
            expected,
            allowed_event_types=normalized_allowed_types,
            event_allowlist=normalized_allowlist,
        )
        manifest["cn_masterdata_version"] = str(cn_masterdata_version)
        manifest["jp_masterdata_version"] = str(jp_masterdata_version)
        manifest["build_policy"] = {
            "allowed_event_types": list(normalized_allowed_types),
            "event_allowlist": list(normalized_allowlist),
        }

        fingerprint_input = get_preview_manifest_fingerprint_input(manifest)
        scope_fingerprint = _sha256_bytes(_canonical_json_bytes(fingerprint_input))
        manifest["scope_fingerprint"] = scope_fingerprint

        masterdata_dir = staging_dir / "masterdata"
        for filename in expected:
            _atomic_write(masterdata_dir / filename, outputs[filename])

        if run_canaries:
            manifest["canary"] = run_native_canaries(
                masterdata_dir,
                musicmetas_path,
                manifest,
            )
        else:
            manifest["canary"] = {"cold_load": False, "skipped": True}

        verify_sources_unchanged(snapshot)
        _atomic_write(staging_dir / "manifest.json", _canonical_json_bytes(manifest))

        version_token = scope_fingerprint.removeprefix("sha256:")
        versions_dir = root / "versions"
        version_dir = versions_dir / version_token
        versions_dir.mkdir(parents=True, exist_ok=True)
        if version_dir.exists():
            _safe_remove_tree(staging_dir, staging_parent)
        else:
            os.replace(staging_dir, version_dir)

        pointer = {
            "scope_fingerprint": scope_fingerprint,
            "version_dir": str(Path("versions") / version_token).replace("\\", "/"),
        }
        _atomic_write(root / "current.json", _canonical_json_bytes(pointer))
        return RulesetBuildResult(
            scope_fingerprint=scope_fingerprint,
            version_dir=str(version_dir),
            manifest_path=str(version_dir / "manifest.json"),
            event_ids=tuple(manifest["event_ids"]),
            future_card_ids=tuple(manifest["future_cards"]["ids"]),
            reused=False,
        )
    finally:
        if staging_dir.exists():
            _safe_remove_tree(staging_dir, staging_parent)
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def load_current_manifest(output_root: str) -> tuple[dict[str, Any], Path]:
    """读取并校验当前原子指针，供调度器和命令路由使用。"""

    root = Path(output_root).resolve()
    pointer = json.loads((root / "current.json").read_text(encoding="utf-8"))
    version_dir = (root / pointer["version_dir"]).resolve()
    if root not in version_dir.parents:
        raise RulesetBuildError("current.json 指向规则集目录之外")
    manifest_path = version_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("scope_fingerprint") != pointer.get("scope_fingerprint"):
        raise RulesetBuildError("current.json 与 manifest 指纹不一致")
    return manifest, version_dir
