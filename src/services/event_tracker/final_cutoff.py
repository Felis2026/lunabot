"""历史最终线的共享档位能力与完整性判定。"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Optional


# ================================ 受支持档位 ================================ #

FINAL_CUTOFF_SUPPORTED_RANKS = (
    *range(1, 100),
    *range(100, 501, 100),
    *range(1000, 5001, 1000),
    1500,
    2500,
    *range(10000, 50001, 10000),
    *range(100000, 500001, 100000),
)

# 总榜和 WL 章节榜来自不同的 border 结构，不能用同一组档位判断完整性。
# 当前总榜接口：CN 稳定到 T200000，JP 稳定到 T300000。
OVERALL_MAX_RANK_BY_REGION = {
    "cn": 200000,
    "jp": 300000,
}

# WL 章节榜稳定到 T50000，且不提供总榜特有的 T1500/T2500。
CHAPTER_MAX_RANK = 50000
CHAPTER_UNSUPPORTED_RANKS = frozenset({1500, 2500})


# ================================ 结榜确认采样 ================================ #

@dataclass(frozen=True)
class FinalCutoffSample:
    """一次固定检查点的完整榜线观测，分数只包含该榜单要求的档位。"""

    interval_seconds: int
    slot: int
    observed_at_ms: int
    complete: bool
    scores: dict[int, int]


def get_final_cutoff_sample_slot(
    aggregate_at_ms: int,
    observed_at_ms: int,
    interval_seconds: int,
) -> Optional[int]:
    """
    把结榜后的观测映射到固定检查槽；首槽从一个完整间隔后开始。

    固定槽位避免进程重启后重新从零计时，也保证同一 30 分钟窗口最多落一份样本。
    """

    if interval_seconds <= 0:
        raise ValueError("最终榜线采样间隔必须大于 0")
    # 项目原有结束时间统一按 aggregateAt + 1 秒解释，这里必须保持一致。
    elapsed_ms = int(observed_at_ms) - (int(aggregate_at_ms) + 1000)
    interval_ms = int(interval_seconds) * 1000
    if elapsed_ms < interval_ms:
        return None
    return int(elapsed_ms // interval_ms)


def get_stable_final_cutoff_sample(
    samples: Iterable[FinalCutoffSample],
    required_count: int,
) -> Optional[FinalCutoffSample]:
    """连续槽位中的最近 N 份完整快照完全一致时，返回最后一份确认样本。"""

    if required_count <= 0:
        raise ValueError("最终榜线稳定样本数必须大于 0")
    ordered = sorted(samples, key=lambda sample: sample.slot)
    if len(ordered) < required_count:
        return None
    recent = ordered[-required_count:]
    expected_slots = list(range(
        recent[-1].slot - required_count + 1,
        recent[-1].slot + 1,
    ))
    if [sample.slot for sample in recent] != expected_slots:
        return None
    if not all(sample.complete for sample in recent):
        return None
    if len({sample.interval_seconds for sample in recent}) != 1:
        return None
    interval_ms = recent[0].interval_seconds * 1000
    if any(
        current.observed_at_ms - previous.observed_at_ms < interval_ms
        for previous, current in zip(recent, recent[1:])
    ):
        return None
    first_scores = recent[0].scores
    if any(sample.scores != first_scores for sample in recent[1:]):
        return None
    return recent[-1]


def get_expected_final_cutoff_ranks(
    region: str,
    supported_ranks: Iterable[int] = FINAL_CUTOFF_SUPPORTED_RANKS,
    *,
    board: Mapping[str, Any] | str | None = None,
) -> list[int]:
    """按区服与榜单类型返回该来源实际应具备的最终线档位。"""

    normalized_ranks = sorted({int(rank) for rank in supported_ranks})
    board_kind = (
        str(board.get("kind", "overall"))
        if isinstance(board, Mapping)
        else str(board or "overall")
    )
    if board_kind == "chapter":
        return [
            rank
            for rank in normalized_ranks
            if rank <= CHAPTER_MAX_RANK
            and rank not in CHAPTER_UNSUPPORTED_RANKS
        ]

    max_rank = OVERALL_MAX_RANK_BY_REGION.get(region.lower())
    if max_rank is None:
        return normalized_ranks
    return [rank for rank in normalized_ranks if rank <= max_rank]


def classify_final_cutoff_ranks(
    region: str,
    board: Mapping[str, Any] | str | None,
    actual_ranks: Iterable[int],
) -> tuple[str, list[int]]:
    """返回规范化状态与缺失档位，供运行时、导入和修复脚本共用。"""

    expected = set(get_expected_final_cutoff_ranks(region, board=board))
    actual = {int(rank) for rank in actual_ranks}
    missing = sorted(expected - actual)
    return ("complete" if not missing else "partial"), missing


# ================================ 最终线快照结构 ================================ #

FINAL_CUTOFF_DATA_DIR = Path("data/sekai/final_cutoffs")
FINAL_CUTOFF_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class FinalCutoffItem:
    """一个最终榜线档位。"""

    rank: int
    score: int


@dataclass(frozen=True)
class FinalCutoffSnapshot:
    """一个活动总榜或 WL 章节榜的最终快照。"""

    schema_version: int
    region: str
    event_id: int
    event_name: str
    event_type: str
    board: dict[str, Any]
    legacy_tracking_id: int
    start_at_ms: int
    aggregate_at_ms: int
    finalized_at_ms: int
    source: dict[str, Any]
    status: str
    missing_ranks: list[int]
    cutoffs: list[FinalCutoffItem]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FinalCutoffSnapshot":
        """校验并解析磁盘 JSON，拒绝静默读取未知 schema 或重复档位。"""

        schema_version = int(data.get("schema_version", 0))
        if schema_version != FINAL_CUTOFF_SCHEMA_VERSION:
            raise ValueError(f"不支持的历史榜线 schema_version={schema_version}")

        cutoffs = [
            FinalCutoffItem(rank=int(item["rank"]), score=int(item["score"]))
            for item in data.get("cutoffs", [])
        ]
        ranks = [item.rank for item in cutoffs]
        if not cutoffs:
            raise ValueError("历史榜线快照不包含任何档位")
        if len(ranks) != len(set(ranks)):
            raise ValueError("历史榜线快照包含重复档位")
        if any(rank <= 0 for rank in ranks):
            raise ValueError("历史榜线快照包含无效档位")
        if any(item.score < 0 for item in cutoffs):
            raise ValueError("历史榜线快照包含负分")

        cutoffs.sort(key=lambda item: item.rank)
        return cls(
            schema_version=schema_version,
            region=str(data["region"]).lower(),
            event_id=int(data["event_id"]),
            event_name=str(data.get("event_name", "")),
            event_type=str(data.get("event_type", "")),
            board=dict(data.get("board") or {"kind": "overall"}),
            legacy_tracking_id=int(data.get("legacy_tracking_id", data["event_id"])),
            start_at_ms=int(data.get("start_at_ms", 0)),
            aggregate_at_ms=int(data.get("aggregate_at_ms", 0)),
            finalized_at_ms=int(data.get("finalized_at_ms", 0)),
            source=dict(data.get("source") or {}),
            status=str(data.get("status", "unverified")),
            missing_ranks=sorted(int(rank) for rank in data.get("missing_ranks", [])),
            cutoffs=cutoffs,
        )

    def to_dict(self) -> dict[str, Any]:
        """转换为稳定、可审计的 JSON 结构。"""

        return {
            "schema_version": self.schema_version,
            "region": self.region,
            "event_id": self.event_id,
            "event_name": self.event_name,
            "event_type": self.event_type,
            "board": self.board,
            "legacy_tracking_id": self.legacy_tracking_id,
            "start_at_ms": self.start_at_ms,
            "aggregate_at_ms": self.aggregate_at_ms,
            "finalized_at_ms": self.finalized_at_ms,
            "source": self.source,
            "status": self.status,
            "missing_ranks": self.missing_ranks,
            "cutoffs": [
                {"rank": item.rank, "score": item.score}
                for item in self.cutoffs
            ],
        }


# ================================ 完整性与升级规则 ================================ #

def normalize_final_cutoff_snapshot(
    snapshot: FinalCutoffSnapshot,
) -> FinalCutoffSnapshot:
    """按共享能力表重算状态，修复旧脚本写出的错误 partial/complete 元数据。"""

    status, missing_ranks = classify_final_cutoff_ranks(
        snapshot.region,
        snapshot.board,
        (item.rank for item in snapshot.cutoffs),
    )
    if (
        snapshot.status == status
        and snapshot.missing_ranks == missing_ranks
    ):
        return snapshot
    return replace(
        snapshot,
        status=status,
        missing_ranks=missing_ranks,
    )


def should_replace_final_cutoff_snapshot(
    existing: Optional[FinalCutoffSnapshot],
    candidate: FinalCutoffSnapshot,
) -> bool:
    """
    判断新的运行时快照是否可以原子替换旧快照。

    complete 快照一经确认即冻结；partial 只允许档位集合不减少的升级，
    同档位分数也只有在候选记录时间更晚时才允许刷新。
    """

    if existing is None:
        return True
    if existing.status == "complete":
        return False

    existing_scores = {
        item.rank: item.score
        for item in existing.cutoffs
    }
    candidate_scores = {
        item.rank: item.score
        for item in candidate.cutoffs
    }
    if not set(existing_scores).issubset(candidate_scores):
        return False
    if existing.status != "complete" and candidate.status == "complete":
        return True
    if len(candidate_scores) > len(existing_scores):
        return True
    return (
        candidate.finalized_at_ms > existing.finalized_at_ms
        and candidate_scores != existing_scores
    )


def is_final_cutoff_ready_for_archive(
    snapshot: Optional[FinalCutoffSnapshot],
) -> bool:
    """只有实际档位完整的快照才允许进入不可逆的 SQLite 清理阶段。"""

    return (
        snapshot is not None
        and normalize_final_cutoff_snapshot(snapshot).status == "complete"
    )


# ================================ 路径与读写 ================================ #

def get_final_cutoff_path(
    region: str,
    event_id: int,
    chapter_no: Optional[int] = None,
) -> Path:
    """生成总榜或章节榜快照路径。"""

    filename = (
        "overall.json"
        if chapter_no is None
        else f"chapter-{int(chapter_no)}.json"
    )
    return (
        FINAL_CUTOFF_DATA_DIR
        / region.lower()
        / str(int(event_id))
        / filename
    )


def load_final_cutoff_snapshot(
    region: str,
    event_id: int,
    chapter_no: Optional[int] = None,
) -> Optional[FinalCutoffSnapshot]:
    """读取历史最终线；文件不存在时返回 None。"""

    path = get_final_cutoff_path(region, event_id, chapter_no)
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as file:
        snapshot = FinalCutoffSnapshot.from_dict(json.load(file))

    expected_chapter_no = int(chapter_no) if chapter_no is not None else None
    actual_chapter_no = (
        snapshot.board.get("chapter_no")
        if snapshot.board.get("kind") == "chapter"
        else None
    )
    if (
        snapshot.region != region.lower()
        or snapshot.event_id != int(event_id)
        or actual_chapter_no != expected_chapter_no
    ):
        raise ValueError("历史榜线快照内容与文件路径不一致")

    normalized = normalize_final_cutoff_snapshot(snapshot)
    if normalized != snapshot:
        # 旧修复脚本曾使用总榜档位判断所有榜单，导致正确快照长期标成
        # partial。读取时只迁移状态元数据，不改动任何实际分数。
        _atomic_write_snapshot(path, normalized)
    return normalized


def _atomic_write_snapshot(
    path: Path,
    snapshot: FinalCutoffSnapshot,
) -> None:
    """把一份已经校验的快照原子写入指定路径。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8", newline="\n") as file:
        json.dump(snapshot.to_dict(), file, ensure_ascii=False, indent=2)
        file.write("\n")
    os.replace(tmp_path, path)


def save_final_cutoff_snapshot(snapshot: FinalCutoffSnapshot) -> Path:
    """原子写入最终线，避免进程中断留下半份 JSON。"""

    snapshot = normalize_final_cutoff_snapshot(snapshot)
    chapter_no = (
        snapshot.board.get("chapter_no")
        if snapshot.board.get("kind") == "chapter"
        else None
    )
    path = get_final_cutoff_path(snapshot.region, snapshot.event_id, chapter_no)
    _atomic_write_snapshot(path, snapshot)
    return path
