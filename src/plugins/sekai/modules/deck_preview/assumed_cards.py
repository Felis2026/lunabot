from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence


class PreviewCardError(ValueError):
    """未来卡假设或结果集合越界。"""


@dataclass(frozen=True)
class AssumedCardsContext:
    """一次请求的真实、假设与实际提交卡牌集合。"""

    actual_cn_card_ids: frozenset[int]
    assumed_card_ids: frozenset[int]
    explicit_cn_catalog_card_ids: frozenset[int]
    submitted_card_ids: frozenset[int]

    @property
    def allowed_card_ids(self) -> frozenset[int]:
        return (
            self.actual_cn_card_ids
            | self.assumed_card_ids
            | self.explicit_cn_catalog_card_ids
        )


def select_result_card_masterdata(
    card_id: int,
    context: AssumedCardsContext | None,
    *,
    cn_card: Mapping[str, Any] | None,
    jp_card: Mapping[str, Any] | None,
) -> Mapping[str, Any]:
    """
    为组卡结果选择对应区服的卡牌元数据。

    只有本次显式假设持有的 JP 未实装卡使用 JP 元数据；CN 已上线卡牌即使
    Suite 尚未持有，也必须继续使用 CN 元数据，避免两个卡库被无条件混用。
    """

    assumed = bool(context and card_id in context.assumed_card_ids)
    selected = jp_card if assumed else cn_card
    if not isinstance(selected, Mapping):
        source = "JP预演" if assumed else "CN"
        raise PreviewCardError(f"找不到组卡结果卡牌 {card_id} 的{source}资料")
    return selected


def _default_user_card(
    card: Mapping[str, Any],
    episodes: Sequence[Mapping[str, Any]],
    config_override: Mapping[str, bool],
) -> dict[str, Any]:
    rarity = card.get("cardRarityType")
    max_levels = {
        "rarity_1": 20,
        "rarity_2": 30,
        "rarity_3": 50,
        "rarity_4": 60,
        "rarity_birthday": 60,
    }
    if rarity not in max_levels:
        raise PreviewCardError(f"未来卡 {card.get('id')} 的稀有度无法识别")
    trained = rarity in {"rarity_3", "rarity_4"}
    episodes_read = bool(config_override.get("episode_read"))
    return {
        "cardId": card["id"],
        "level": max_levels[rarity],
        "skillLevel": 4 if config_override.get("skill_max") else 1,
        "masterRank": 5 if config_override.get("master_max") else 0,
        "specialTrainingStatus": "done" if trained else "none",
        "defaultImage": "special_training" if trained else "original",
        "episodes": (
            [
                {
                    "cardEpisodeId": episode["id"],
                    "scenarioStatus": "already_read",
                    "isNotSkipped": False,
                }
                for episode in episodes
            ]
            if episodes_read
            else []
        ),
    }


def inject_explicit_assumed_cards(
    profile: dict[str, Any],
    *,
    fixed_card_ids: Iterable[int],
    cn_catalog_card_ids: set[int] | None = None,
    future_card_ids: set[int],
    jp_cards_by_id: Mapping[int, Mapping[str, Any]],
    jp_episodes_by_card_id: Mapping[int, Sequence[Mapping[str, Any]]],
    max_assumed_cards: int,
    card_config_overrides: Mapping[int, Mapping[str, bool]] | None = None,
) -> AssumedCardsContext:
    """
    只为用户通过 `#ID` 固定的 JP 未实装卡创建本次临时 userCard。

    不写回 Suite，不把未来卡加入自由候选池，也不要求它属于目标活动。
    """

    user_cards = profile.get("userCards")
    if not isinstance(user_cards, list):
        raise PreviewCardError("Suite 缺少 userCards，无法进行预演")
    actual_ids = {
        item.get("cardId")
        for item in user_cards
        if isinstance(item, dict) and isinstance(item.get("cardId"), int)
    }
    fixed_ids = [int(card_id) for card_id in fixed_card_ids]
    if len(fixed_ids) != len(set(fixed_ids)):
        raise PreviewCardError("固定卡牌不能重复")
    if max_assumed_cards < 0:
        raise PreviewCardError("max_assumed_cards 不能小于 0")
    config_overrides = card_config_overrides or {}
    cn_catalog_ids = cn_catalog_card_ids or set()
    assumed_ids: list[int] = []
    explicit_cn_catalog_ids: list[int] = []

    # ================================ 假设卡闭包预检 ================================ #
    pending_cards: list[
        tuple[int, Mapping[str, Any], Sequence[Mapping[str, Any]]]
    ] = []
    for card_id in fixed_ids:
        if card_id in actual_ids:
            continue
        if card_id in cn_catalog_ids:
            # 原生组卡允许固定一张 Suite 未持有、但已存在于正式 CN 卡库的卡。
            # 它通过 fixed_cards 参数显式提交，不应被未来卡结果保护误判为越界。
            explicit_cn_catalog_ids.append(card_id)
            continue
        if card_id not in future_card_ids:
            raise PreviewCardError(
                f"固定卡牌 {card_id} 不在你的 CN Suite，也不属于已校验的 JP 未实装卡"
            )
        card = jp_cards_by_id.get(card_id)
        episodes = jp_episodes_by_card_id.get(card_id, ())
        if card is None or not episodes:
            raise PreviewCardError(f"未来卡 {card_id} 的 JP 资料闭包不完整")
        pending_cards.append((card_id, card, episodes))

    if len(pending_cards) > max_assumed_cards:
        raise PreviewCardError(
            f"固定卡牌数量不能超过{max_assumed_cards}张"
        )

    # ================================ 临时Suite注入 ================================ #
    # 所有检查通过后才修改本次 profile，避免异常路径留下半份假设卡。
    for card_id, card, episodes in pending_cards:
        user_card = _default_user_card(
            card,
            episodes,
            config_overrides.get(card_id, {}),
        )
        assumed_ids.append(card_id)
        user_cards.append(user_card)

    submitted_ids = {
        item.get("cardId")
        for item in user_cards
        if isinstance(item, dict) and isinstance(item.get("cardId"), int)
    }
    return AssumedCardsContext(
        actual_cn_card_ids=frozenset(actual_ids),
        assumed_card_ids=frozenset(assumed_ids),
        explicit_cn_catalog_card_ids=frozenset(explicit_cn_catalog_ids),
        submitted_card_ids=frozenset(submitted_ids),
    )


def validate_preview_result_cards(
    result_decks: Iterable[Any],
    context: AssumedCardsContext,
    *,
    fixed_card_ids: Iterable[int],
) -> None:
    """结果必须同时属于授权集合和本次真正提交给原生后端的 userCards。"""

    fixed_ids = {int(card_id) for card_id in fixed_card_ids}
    if not context.assumed_card_ids <= fixed_ids:
        raise PreviewCardError("假设未来卡没有全部出现在用户显式固定卡中")
    if not context.explicit_cn_catalog_card_ids <= fixed_ids:
        raise PreviewCardError("Suite 未持有的 CN 卡没有全部出现在用户显式固定卡中")
    for deck in result_decks:
        for card in deck.cards:
            card_id = int(card.card_id)
            if card_id not in context.allowed_card_ids:
                raise PreviewCardError(f"预演结果出现未授权卡牌 {card_id}")
            if (
                card_id not in context.submitted_card_ids
                and card_id not in context.explicit_cn_catalog_card_ids
            ):
                raise PreviewCardError(f"预演结果出现未提交卡牌 {card_id}")
