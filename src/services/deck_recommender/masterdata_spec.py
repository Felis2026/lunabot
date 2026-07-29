import os
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence


DEFAULT_NATIVE_PACKAGE_BUILD_ID = "upstream"


def get_native_package_build_id() -> str:
    """返回区分同版本不同原生二进制的稳定身份，参与 preview 缓存与同步校验。"""

    return (
        os.getenv("DECKREC_NATIVE_BUILD_ID", "").strip()
        or DEFAULT_NATIVE_PACKAGE_BUILD_ID
    )


@dataclass(frozen=True)
class DeckMasterDataSpec:
    """描述组卡服务消费的一份 MasterData，文件名必须与上游缓存一致。"""

    attribute: str
    filename: str
    capability: str = "base"


# ================================ 正式组卡基础文件 ================================ #
# 这份规格是组卡客户端、preview builder 和服务端 v2 校验的共同真源。
BASE_DECK_MASTERDATA_SPECS: tuple[DeckMasterDataSpec, ...] = (
    DeckMasterDataSpec("area_item_levels", "areaItemLevels.json"),
    DeckMasterDataSpec("area_items", "areaItems.json"),
    DeckMasterDataSpec("areas", "areas.json"),
    DeckMasterDataSpec("card_episodes", "cardEpisodes.json"),
    DeckMasterDataSpec("cards", "cards.json"),
    DeckMasterDataSpec("card_rarities", "cardRarities.json"),
    DeckMasterDataSpec("character_ranks", "characterRanks.json"),
    DeckMasterDataSpec("event_cards", "eventCards.json"),
    DeckMasterDataSpec("event_deck_bonuses", "eventDeckBonuses.json"),
    DeckMasterDataSpec("event_exchange_summaries", "eventExchangeSummaries.json"),
    DeckMasterDataSpec("events", "events.json"),
    DeckMasterDataSpec("event_items", "eventItems.json"),
    DeckMasterDataSpec("event_rarity_bonus_rates", "eventRarityBonusRates.json"),
    DeckMasterDataSpec("game_characters", "gameCharacters.json"),
    DeckMasterDataSpec("game_character_units", "gameCharacterUnits.json"),
    DeckMasterDataSpec("honors", "honors.json"),
    DeckMasterDataSpec("master_lessons", "masterLessons.json"),
    DeckMasterDataSpec("music_diffs", "musicDifficulties.json"),
    DeckMasterDataSpec("musics", "musics.json"),
    DeckMasterDataSpec("music_vocals", "musicVocals.json"),
    DeckMasterDataSpec("shop_items", "shopItems.json"),
    DeckMasterDataSpec("skills", "skills.json"),
    DeckMasterDataSpec(
        "world_bloom_different_attribute_bonuses",
        "worldBloomDifferentAttributeBonuses.json",
    ),
    DeckMasterDataSpec("world_blooms", "worldBlooms.json"),
    DeckMasterDataSpec(
        "world_bloom_support_deck_bonuses",
        "worldBloomSupportDeckBonuses.json",
    ),
)


# ================================ 可选能力文件 ================================ #

MYSEKAI_DECK_MASTERDATA_SPECS: tuple[DeckMasterDataSpec, ...] = (
    DeckMasterDataSpec(
        "card_mysekai_canvas_bonuses",
        "cardMysekaiCanvasBonuses.json",
        "mysekai",
    ),
    DeckMasterDataSpec(
        "mysekai_fixture_game_character_groups",
        "mysekaiFixtureGameCharacterGroups.json",
        "mysekai",
    ),
    DeckMasterDataSpec(
        "mysekai_fixture_game_character_group_performance_bonuses",
        "mysekaiFixtureGameCharacterGroupPerformanceBonuses.json",
        "mysekai",
    ),
    DeckMasterDataSpec("mysekai_gates", "mysekaiGates.json", "mysekai"),
    DeckMasterDataSpec("mysekai_gate_levels", "mysekaiGateLevels.json", "mysekai"),
)

WL_LIMITED_DECK_MASTERDATA_SPECS: tuple[DeckMasterDataSpec, ...] = (
    DeckMasterDataSpec(
        "world_bloom_support_deck_unit_event_limited_bonuses",
        "worldBloomSupportDeckUnitEventLimitedBonuses.json",
        "wl_limited_bonus",
    ),
)


def get_deck_masterdata_specs(
    *,
    include_mysekai: bool,
    include_wl_limited_bonus: bool,
) -> tuple[DeckMasterDataSpec, ...]:
    """按区服已开放能力返回稳定、有序且无重复的组卡文件规格。"""

    specs: list[DeckMasterDataSpec] = list(BASE_DECK_MASTERDATA_SPECS)
    if include_mysekai:
        specs.extend(MYSEKAI_DECK_MASTERDATA_SPECS)
    if include_wl_limited_bonus:
        specs.extend(WL_LIMITED_DECK_MASTERDATA_SPECS)

    filenames = [spec.filename for spec in specs]
    if len(filenames) != len(set(filenames)):
        raise ValueError("组卡 MasterData 规格中存在重复文件名")
    return tuple(specs)


def get_spec_filenames(specs: Iterable[DeckMasterDataSpec]) -> tuple[str, ...]:
    """提取规格中的稳定文件名序列，供同步协议和测试共同使用。"""

    return tuple(spec.filename for spec in specs)


def get_preview_expected_filenames() -> tuple[str, ...]:
    """Preview 固定启用当前 CN 已开放的 MySekai 与 WL 限定表，共 31 份。"""

    return get_spec_filenames(get_deck_masterdata_specs(
        include_mysekai=True,
        include_wl_limited_bonus=True,
    ))


def get_preview_manifest_fingerprint_input(
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    """返回决定 preview 数据内容与路由语义的完整稳定指纹输入。"""

    return {
        "schema_version": manifest.get("schema_version"),
        "data_scope": manifest.get("data_scope"),
        "native_region": manifest.get("native_region"),
        "cn_masterdata_version": manifest.get("cn_masterdata_version"),
        "jp_masterdata_version": manifest.get("jp_masterdata_version"),
        "event_ids": manifest.get("event_ids"),
        "event_types": manifest.get("event_types"),
        "world_bloom_chapters": manifest.get("world_bloom_chapters"),
        "global_overrides": manifest.get("global_overrides"),
        "future_cards": manifest.get("future_cards"),
        "excluded_events": manifest.get("excluded_events"),
        "files": manifest.get("files"),
        "source_files": manifest.get("source_files"),
        "build_policy": manifest.get("build_policy"),
        "builder_version": manifest.get("builder_version"),
        "native_package_version": manifest.get("native_package_version"),
        "native_package_build_id": manifest.get("native_package_build_id"),
    }


def validate_resolved_paths(
    specs: Sequence[DeckMasterDataSpec],
    paths: Sequence[str],
) -> None:
    """检查资源管理器实际解析出的文件与共享规格一一对应。"""

    if len(specs) != len(paths):
        raise ValueError(
            f"组卡 MasterData 路径数量不匹配: expected={len(specs)} actual={len(paths)}"
        )
