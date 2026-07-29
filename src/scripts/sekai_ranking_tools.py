"""Sekai 榜线归档、最终线和预测历史的离线维护工具。"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.services.event_tracker.final_cutoff import (
    FINAL_CUTOFF_SUPPORTED_RANKS,
    classify_final_cutoff_ranks,
)


SCHEMA_VERSION = 1


# ================================ 通用校验工具 ================================ #

def sha256_file(path: Path) -> str:
    """流式计算文件 SHA-256，避免大数据库一次性读入内存。"""

    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def database_stats(path: Path, *, immutable: bool) -> dict[str, Any]:
    """读取完整性、行数、末尾记录和最终档位。"""

    if immutable:
        connection = sqlite3.connect(
            f"file:{path.as_posix()}?mode=ro&immutable=1",
            uri=True,
        )
    else:
        connection = sqlite3.connect(path, timeout=30)
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        row_count, max_id, max_ts = connection.execute(
            "SELECT COUNT(*), MAX(id), MAX(ts) FROM ranking"
        ).fetchone()
        cutoffs = connection.execute("""
            SELECT rank, score, ts
            FROM ranking
            WHERE id IN (SELECT MAX(id) FROM ranking GROUP BY rank)
            ORDER BY rank
        """).fetchall()
    finally:
        connection.close()
    return {
        "integrity": str(integrity),
        "row_count": int(row_count or 0),
        "max_id": int(max_id) if max_id is not None else None,
        "max_ts": float(max_ts) if max_ts is not None else None,
        "cutoffs": [
            {
                "rank": int(rank),
                "score": int(score),
                "ts": float(timestamp),
            }
            for rank, score, timestamp in cutoffs
        ],
    }


def extract_database(zip_path: Path, output_dir: Path, db_name: str) -> Path:
    """从 ZIP 中安全提取唯一的目标数据库成员。"""

    with zipfile.ZipFile(zip_path) as archive:
        matching_members = [
            member for member in archive.namelist()
            if Path(member).name == db_name
        ]
        if len(matching_members) != 1:
            raise ValueError(f"{zip_path} 中未找到唯一的 {db_name}")
        output_path = output_dir / db_name
        with archive.open(matching_members[0]) as source, output_path.open("wb") as target:
            shutil.copyfileobj(source, target)
    return output_path


def create_and_verify_zip(
    database_path: Path,
    candidate_zip: Path,
    expected_stats: dict[str, Any],
    verify_dir: Path,
) -> dict[str, Any]:
    """创建候选 ZIP，再从 ZIP 解压并独立复检。"""

    with zipfile.ZipFile(
        candidate_zip,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as archive:
        archive.write(database_path, arcname=database_path.name)

    with zipfile.ZipFile(candidate_zip) as archive:
        bad_member = archive.testzip()
        if bad_member:
            raise ValueError(f"ZIP CRC校验失败：{bad_member}")
    verified_database = extract_database(candidate_zip, verify_dir, database_path.name)
    verified_stats = database_stats(verified_database, immutable=True)
    for key in ("integrity", "row_count", "max_id", "max_ts"):
        if verified_stats[key] != expected_stats[key]:
            raise ValueError(
                f"候选ZIP复检不一致：{key} "
                f"expected={expected_stats[key]} actual={verified_stats[key]}"
            )
    return verified_stats


# ================================ MasterData与最终线 ================================ #

def load_json(path: Optional[str | Path]) -> Any:
    """读取 UTF-8 JSON；兼容部分外部缓存携带的 BOM。"""

    if not path:
        return []
    with Path(path).open("r", encoding="utf-8-sig") as file:
        return json.load(file)


def find_by(items: list[dict], key: str, value: Any) -> Optional[dict]:
    return next((item for item in items if item.get(key) == value), None)


def build_final_snapshot(
    region: str,
    tracking_id: int,
    stats: dict[str, Any],
    events: list[dict],
    world_blooms: list[dict],
    source: dict[str, Any],
    freshness_seconds: int,
) -> tuple[Optional[dict], Optional[str]]:
    """把修复后的数据库转换为版本化最终线；末尾记录过旧时拒绝生成。"""

    base_event_id = tracking_id % 1000
    chapter_no = tracking_id // 1000
    event = find_by(events, "id", base_event_id)
    if not event:
        return None, f"MasterData 中缺少活动 {base_event_id}"

    if chapter_no:
        chapter = next((
            item for item in world_blooms
            if item.get("eventId") == base_event_id
            and item.get("chapterNo") == chapter_no
        ), None)
        if not chapter:
            return None, f"MasterData 中缺少 WL 榜单 {tracking_id}"
        start_at_ms = int(chapter["chapterStartAt"])
        aggregate_at_ms = int(chapter["aggregateAt"])
        board = {
            "kind": "chapter",
            "chapter_no": chapter_no,
            "game_character_id": chapter.get("gameCharacterId"),
            "chapter_type": chapter.get("worldBloomChapterType", "game_character"),
            "is_supplemental": chapter.get("isSupplemental", False),
        }
    else:
        start_at_ms = int(event["startAt"])
        aggregate_at_ms = int(event["aggregateAt"])
        board = {"kind": "overall"}

    max_ts = stats.get("max_ts")
    if max_ts is None:
        return None, "数据库没有榜线记录"
    seconds_before_end = aggregate_at_ms / 1000 + 1 - max_ts
    if seconds_before_end > freshness_seconds:
        return None, f"最后记录早于结榜 {seconds_before_end:.1f} 秒"

    cutoff_by_rank = {
        int(item["rank"]): int(item["score"])
        for item in stats["cutoffs"]
        if int(item["rank"]) in FINAL_CUTOFF_SUPPORTED_RANKS
    }
    if not cutoff_by_rank:
        return None, "数据库没有受支持的最终榜线档位"
    status, missing_ranks = classify_final_cutoff_ranks(
        region,
        board,
        cutoff_by_rank,
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "region": region,
        "event_id": base_event_id,
        "event_name": event.get("name", ""),
        "event_type": event.get("eventType", ""),
        "board": board,
        "legacy_tracking_id": tracking_id,
        "start_at_ms": start_at_ms,
        "aggregate_at_ms": aggregate_at_ms,
        "finalized_at_ms": int(max_ts * 1000),
        "source": source,
        "status": status,
        "missing_ranks": missing_ranks,
        "cutoffs": [
            {"rank": rank, "score": cutoff_by_rank[rank]}
            for rank in sorted(cutoff_by_rank)
        ],
    }, None


def write_snapshot(
    output_root: Path,
    snapshot: dict,
    *,
    overwrite: bool,
) -> tuple[Path, bool]:
    """原子写入总榜或章节最终线，不默认覆盖既有人工/导入数据。"""

    board = snapshot["board"]
    filename = (
        "overall.json"
        if board["kind"] == "overall"
        else f"chapter-{board['chapter_no']}.json"
    )
    path = output_root / snapshot["region"] / str(snapshot["event_id"]) / filename
    if path.exists() and not overwrite:
        return path, False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8", newline="\n") as file:
        json.dump(snapshot, file, ensure_ascii=False, indent=2)
        file.write("\n")
    os.replace(tmp_path, path)
    return path, True


# ================================ 单归档修复 ================================ #

def repair_one(
    args: argparse.Namespace,
    tracking_id: int,
    events: list[dict],
    world_blooms: list[dict],
) -> dict[str, Any]:
    """修复一个 ZIP+WAL 组合；正式替换仅在 --apply 时执行。"""

    db_dir = Path(args.db_dir)
    db_name = f"{tracking_id}_ranking.db"
    zip_path = db_dir / f"{db_name}.zip"
    wal_path = db_dir / f"{db_name}-wal"
    shm_path = db_dir / f"{db_name}-shm"
    if not zip_path.exists() or not wal_path.exists():
        raise FileNotFoundError(f"缺少 {zip_path.name} 或 {wal_path.name}")

    source_files = [zip_path, wal_path]
    if shm_path.exists():
        source_files.append(shm_path)
    source_hashes = {path.name: sha256_file(path) for path in source_files}

    with tempfile.TemporaryDirectory(
        prefix=f"repair-{tracking_id}-",
        dir=Path(args.work_dir),
    ) as temp_dir_text:
        temp_dir = Path(temp_dir_text)
        recovered_dir = temp_dir / "recovered"
        verify_dir = temp_dir / "verify"
        recovered_dir.mkdir()
        verify_dir.mkdir()

        database_path = extract_database(zip_path, recovered_dir, db_name)
        before_stats = database_stats(database_path, immutable=True)
        shutil.copy2(wal_path, recovered_dir / wal_path.name)

        # 在副本目录正常打开数据库，让 SQLite 回放与其匹配的 WAL。
        connection = sqlite3.connect(database_path, timeout=30)
        try:
            checkpoint = connection.execute(
                "PRAGMA wal_checkpoint(TRUNCATE)"
            ).fetchone()
            if checkpoint and int(checkpoint[0]) != 0:
                raise RuntimeError(f"WAL checkpoint仍被占用：{checkpoint}")
            journal_mode = connection.execute(
                "PRAGMA journal_mode=DELETE"
            ).fetchone()[0]
            if str(journal_mode).lower() != "delete":
                raise RuntimeError(f"无法切换DELETE日志模式：{journal_mode}")
            connection.commit()
        finally:
            connection.close()

        recovered_stats = database_stats(database_path, immutable=True)
        if recovered_stats["integrity"].lower() != "ok":
            raise ValueError(
                f"恢复后integrity_check失败：{recovered_stats['integrity']}"
            )
        residual_wal = recovered_dir / wal_path.name
        if residual_wal.exists() and residual_wal.stat().st_size > 0:
            raise ValueError("checkpoint后仍残留非空WAL")

        candidate_zip = temp_dir / zip_path.name
        verified_stats = create_and_verify_zip(
            database_path,
            candidate_zip,
            recovered_stats,
            verify_dir,
        )
        candidate_hash = sha256_file(candidate_zip)

        result: dict[str, Any] = {
            "tracking_id": tracking_id,
            "applied": False,
            "source_hashes": source_hashes,
            "candidate_zip_sha256": candidate_hash,
            "before": {
                key: before_stats[key]
                for key in ("integrity", "row_count", "max_id", "max_ts")
            },
            "after": {
                key: verified_stats[key]
                for key in ("integrity", "row_count", "max_id", "max_ts")
            },
            "recovered_rows": (
                verified_stats["row_count"] - before_stats["row_count"]
            ),
        }

        if not args.apply:
            return result

        # ================================ 原子发布与隔离备份 ================================ #
        backup_dir = Path(args.backup_dir) / str(tracking_id)
        backup_dir.mkdir(parents=True, exist_ok=False)
        shutil.copy2(zip_path, backup_dir / f"{zip_path.name}.original")

        publish_temp = zip_path.with_suffix(zip_path.suffix + ".repairing")
        if publish_temp.exists():
            publish_temp.unlink()
        shutil.copy2(candidate_zip, publish_temp)
        if sha256_file(publish_temp) != candidate_hash:
            publish_temp.unlink(missing_ok=True)
            raise ValueError("发布临时ZIP的SHA-256与候选文件不一致")
        os.replace(publish_temp, zip_path)

        # ZIP 发布后再把原 WAL/SHM 移入隔离备份；数据仍可恢复，但不会再误导读取端。
        for auxiliary_path in (wal_path, shm_path):
            if auxiliary_path.exists():
                os.replace(auxiliary_path, backup_dir / auxiliary_path.name)

        if sha256_file(zip_path) != candidate_hash:
            raise ValueError("正式ZIP发布后的SHA-256校验失败")
        result["applied"] = True
        result["backup_dir"] = str(backup_dir)

        if args.final_cutoff_dir and events:
            snapshot_source = {
                "kind": "repaired_sqlite_wal_archive",
                "archive_file": zip_path.name,
                "archive_sha256": candidate_hash,
                "recovered_rows": result["recovered_rows"],
                "repaired_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            snapshot, snapshot_issue = build_final_snapshot(
                args.region,
                tracking_id,
                verified_stats,
                events,
                world_blooms,
                snapshot_source,
                args.freshness_seconds,
            )
            if snapshot:
                snapshot_path, written = write_snapshot(
                    Path(args.final_cutoff_dir),
                    snapshot,
                    overwrite=args.overwrite_snapshots,
                )
                result["snapshot_path"] = str(snapshot_path)
                result["snapshot_written"] = written
            else:
                result["snapshot_issue"] = snapshot_issue
        return result


# ================================ 批量入口与报告 ================================ #

def discover_tracking_ids(db_dir: Path) -> list[int]:
    """发现 ZIP 旁存在非空 WAL 的榜线归档。"""

    tracking_ids = []
    for wal_path in db_dir.glob("*_ranking.db-wal"):
        prefix = wal_path.name.removesuffix("_ranking.db-wal")
        zip_path = db_dir / f"{prefix}_ranking.db.zip"
        if prefix.isdigit() and zip_path.exists() and wal_path.stat().st_size > 0:
            tracking_ids.append(int(prefix))
    return sorted(tracking_ids)


def parse_event_ids(text: str) -> list[int]:
    return sorted({int(segment.strip()) for segment in text.split(",") if segment.strip()})


def write_reports(work_dir: Path, report: dict[str, Any]) -> None:
    report_path = work_dir / "repair_report.json"
    with report_path.open("w", encoding="utf-8", newline="\n") as file:
        json.dump(report, file, ensure_ascii=False, indent=2)
        file.write("\n")

    issue_lines = [
        "# 榜线 WAL 归档修复结果",
        "",
        f"生成时间：{report['generated_at']}",
        f"执行模式：{'正式应用' if report['apply'] else '只读演练'}",
        "",
        f"- 成功：{len(report['repaired'])}",
        f"- 失败：{len(report['failed'])}",
        f"- 共恢复 ZIP 外记录：{sum(item['recovered_rows'] for item in report['repaired'])}",
        "",
        "## 修复明细",
        "",
    ]
    for item in report["repaired"]:
        snapshot_text = ""
        if item.get("snapshot_issue"):
            snapshot_text = f"，最终线未生成：{item['snapshot_issue']}"
        elif item.get("snapshot_path"):
            snapshot_text = f"，最终线：{item['snapshot_path']}"
        issue_lines.append(
            f"- {item['tracking_id']}：恢复 {item['recovered_rows']} 行"
            f"{snapshot_text}"
        )
    if report["failed"]:
        issue_lines.extend(["", "## 失败项", ""])
        for item in report["failed"]:
            issue_lines.append(f"- {item['tracking_id']}：{item['error']}")
    (work_dir / "REPAIR_RESULT.md").write_text(
        "\n".join(issue_lines) + "\n",
        encoding="utf-8",
    )


def configure_repair_parser(parser: argparse.ArgumentParser) -> None:
    """注册 WAL 归档修复命令参数。"""

    parser.add_argument("--region", default="cn")
    parser.add_argument("--db-dir", required=True)
    parser.add_argument("--event-ids", help="默认发现所有 ZIP 旁非空 WAL")
    parser.add_argument("--work-dir", required=True)
    parser.add_argument("--backup-dir", required=True)
    parser.add_argument("--events-masterdata")
    parser.add_argument("--world-blooms-masterdata")
    parser.add_argument("--final-cutoff-dir")
    parser.add_argument("--freshness-seconds", type=int, default=600)
    parser.add_argument("--overwrite-snapshots", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.set_defaults(handler=repair_archives_command)


def repair_archives_command(args: argparse.Namespace) -> None:
    """安全回放 ZIP 旁遗留 WAL，并按需原子发布复检后的归档。"""

    work_dir = Path(args.work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    Path(args.backup_dir).mkdir(parents=True, exist_ok=True)
    tracking_ids = (
        parse_event_ids(args.event_ids)
        if args.event_ids
        else discover_tracking_ids(Path(args.db_dir))
    )
    events = load_json(args.events_masterdata)
    world_blooms = load_json(args.world_blooms_masterdata)

    report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "apply": args.apply,
        "region": args.region,
        "tracking_ids": tracking_ids,
        "repaired": [],
        "failed": [],
    }
    for tracking_id in tracking_ids:
        try:
            result = repair_one(args, tracking_id, events, world_blooms)
            report["repaired"].append(result)
            print(
                f"{tracking_id}: recovered_rows={result['recovered_rows']} "
                f"applied={result['applied']}"
            )
        except Exception as exc:
            report["failed"].append({
                "tracking_id": tracking_id,
                "error": f"{type(exc).__name__}: {exc}",
            })
            print(f"{tracking_id}: FAILED {type(exc).__name__}: {exc}")

    write_reports(work_dir, report)
    if report["failed"]:
        raise SystemExit(1)


# ================================ 外部最终线导入 ================================ #

def parse_iso_timestamp_ms(value: str) -> int:
    """把 ISO 8601 时间转换为毫秒时间戳。"""

    normalized = value.replace("Z", "+00:00")
    return int(datetime.fromisoformat(normalized).timestamp() * 1000)


def validate_import_scores(
    scores: dict[str, Any],
) -> tuple[list[dict[str, int]], list[str]]:
    """规范化 rank/score，并验证榜线随排名增大不应上升。"""

    errors: list[str] = []
    cutoffs: list[dict[str, int]] = []
    for raw_rank, raw_score in scores.items():
        try:
            rank = int(raw_rank)
            score = int(raw_score)
        except (TypeError, ValueError):
            errors.append(f"无法解析档位或分数: {raw_rank}={raw_score}")
            continue
        if rank <= 0 or score < 0:
            errors.append(f"无效档位或分数: {rank}={score}")
            continue
        cutoffs.append({"rank": rank, "score": score})

    cutoffs.sort(key=lambda item: item["rank"])
    ranks = [item["rank"] for item in cutoffs]
    if len(ranks) != len(set(ranks)):
        errors.append("存在重复档位")
    for left, right in zip(cutoffs, cutoffs[1:]):
        if right["score"] > left["score"]:
            errors.append(
                f"分数不随排名递减: T{left['rank']}={left['score']} < "
                f"T{right['rank']}={right['score']}"
            )
            break
    return cutoffs, errors


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """原子写入 UTF-8 JSON，避免中断留下半份维护结果。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8", newline="\n") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
        file.write("\n")
    os.replace(tmp_path, path)


def write_import_issue_markdown(
    output_dir: Path,
    report: dict[str, Any],
) -> None:
    """生成给维护者阅读的问题标记；机器读取以 import_manifest.json 为准。"""

    lines = [
        "# JP 历史榜线首批导入问题标记",
        "",
        f"生成时间：{report['generated_at']}",
        "",
        "本文件表示首版导入中被跳过或降级的数据。不要删除；后续补源时按此清单处理。",
        "",
        f"- 导入完整快照：{report['counts']['complete']} 场",
        f"- 导入部分快照：{report['counts']['partial']} 场",
        f"- 跳过异常缓存：{report['counts']['skipped']} 场",
        f"- 完全缺失缓存：{report['counts']['missing']} 场",
        "",
        "## 跳过的异常缓存",
        "",
    ]
    if report["skipped"]:
        for item in report["skipped"]:
            lines.append(f"- JP-{item['event_id']}：{item['reason']}")
    else:
        lines.append("- 无")

    lines.extend(["", "## 完全缺失", ""])
    lines.append(
        "- " + ", ".join(
            f"JP-{event_id}"
            for event_id in report["missing_event_ids"]
        )
        if report["missing_event_ids"]
        else "- 无"
    )

    lines.extend(["", "## 已导入但档位不完整", ""])
    if report["partial"]:
        for item in report["partial"]:
            lines.append(
                f"- JP-{item['event_id']}：{item['rank_count']} 档，"
                f"缺 {len(item['missing_ranks'])} 个当前预期档位"
            )
    else:
        lines.append("- 无")

    path = output_dir / "IMPORT_ISSUES.md"
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp_path, path)


def import_cutoffs(
    source_dir: Path,
    output_dir: Path,
    masterdata_path: Path,
) -> dict[str, Any]:
    """导入 Sekai.best JP 历史缓存，并标记缺失、异常和 partial 快照。"""

    events = {
        int(event["id"]): event
        for event in load_json(masterdata_path)
    }
    source_manifest = load_json(source_dir / "json" / "manifest.json")
    considered_count = int(source_manifest["events_considered"])
    cache_dir = source_dir / ".cache" / "jp"

    report: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": str(source_dir),
        "source_generated_at": source_manifest.get("generated_at"),
        "events_considered": considered_count,
        "imported_event_ids": [],
        "complete_event_ids": [],
        "partial": [],
        "skipped": [],
        "missing_event_ids": [],
    }

    for event_id in range(1, considered_count + 1):
        cache_path = cache_dir / f"event_{event_id}.json"
        if not cache_path.exists():
            report["missing_event_ids"].append(event_id)
            continue

        event = events.get(event_id)
        if not event:
            report["skipped"].append({
                "event_id": event_id,
                "reason": "当前 JP MasterData 中找不到活动",
            })
            continue

        cache = load_json(cache_path)
        try:
            final_timestamp_ms = parse_iso_timestamp_ms(
                cache["final_timestamp"]
            )
        except (KeyError, TypeError, ValueError) as exc:
            report["skipped"].append({
                "event_id": event_id,
                "reason": f"final_timestamp 无效: {exc}",
            })
            continue

        aggregate_at_ms = int(event["aggregateAt"])
        if final_timestamp_ms < aggregate_at_ms:
            delta_minutes = round(
                (aggregate_at_ms - final_timestamp_ms) / 60000,
                2,
            )
            report["skipped"].append({
                "event_id": event_id,
                "reason": f"快照早于结榜 {delta_minutes} 分钟",
            })
            continue

        cutoffs, errors = validate_import_scores(cache.get("scores") or {})
        if errors:
            report["skipped"].append({
                "event_id": event_id,
                "reason": "；".join(errors),
            })
            continue
        if len(cutoffs) < 10:
            report["skipped"].append({
                "event_id": event_id,
                "reason": f"仅有 {len(cutoffs)} 个档位，不进入首版查询",
            })
            continue

        status, missing_ranks = classify_final_cutoff_ranks(
            "jp",
            {"kind": "overall"},
            {item["rank"] for item in cutoffs},
        )
        snapshot = {
            "schema_version": SCHEMA_VERSION,
            "region": "jp",
            "event_id": event_id,
            "event_name": event.get("name", cache.get("event_name", "")),
            "event_type": event.get("eventType", ""),
            "board": {"kind": "overall"},
            "legacy_tracking_id": event_id,
            "start_at_ms": int(event.get("startAt", 0)),
            "aggregate_at_ms": aggregate_at_ms,
            "finalized_at_ms": final_timestamp_ms,
            "source": {
                "kind": "sekai_best_cache_import",
                "cache_file": cache_path.name,
                "cached_at": cache.get("cached_at"),
            },
            "status": status,
            "missing_ranks": missing_ranks,
            "cutoffs": cutoffs,
        }
        atomic_write_json(
            output_dir / str(event_id) / "overall.json",
            snapshot,
        )
        report["imported_event_ids"].append(event_id)
        if status == "complete":
            report["complete_event_ids"].append(event_id)
        else:
            report["partial"].append({
                "event_id": event_id,
                "rank_count": len(cutoffs),
                "missing_ranks": missing_ranks,
            })

    report["counts"] = {
        "complete": len(report["complete_event_ids"]),
        "partial": len(report["partial"]),
        "skipped": len(report["skipped"]),
        "missing": len(report["missing_event_ids"]),
        "imported": len(report["imported_event_ids"]),
    }
    atomic_write_json(output_dir / "import_manifest.json", report)
    write_import_issue_markdown(output_dir, report)
    return report


def configure_import_parser(parser: argparse.ArgumentParser) -> None:
    """注册外部 JP 最终线导入命令参数。"""

    parser.add_argument(
        "--source",
        required=True,
        help="sekai_final_cutoffs_jp_full 目录",
    )
    parser.add_argument(
        "--output",
        default="data/sekai/final_cutoffs/jp",
        help="JP 最终线输出目录",
    )
    parser.add_argument(
        "--masterdata",
        default="data/sekai/assets/masterdata/jp/events.json",
        help="JP events.json 路径",
    )
    parser.set_defaults(handler=import_cutoffs_command)


def import_cutoffs_command(args: argparse.Namespace) -> None:
    """执行外部 JP 最终线导入并打印汇总。"""

    report = import_cutoffs(
        source_dir=Path(args.source).resolve(),
        output_dir=Path(args.output).resolve(),
        masterdata_path=Path(args.masterdata).resolve(),
    )
    print(
        "imported={imported} complete={complete} partial={partial} "
        "skipped={skipped} missing={missing}".format(**report["counts"])
    )


# ================================ 本地归档最终线导出 ================================ #

def discover_archive_tracking_ids(
    db_dir: Path,
    *,
    overall_only: bool,
) -> list[int]:
    """发现可读取的榜线 ZIP；overall_only 时排除旧合成章节 ID。"""

    tracking_ids = []
    for zip_path in db_dir.glob("*_ranking.db.zip"):
        prefix = zip_path.name.removesuffix("_ranking.db.zip")
        if not prefix.isdigit():
            continue
        tracking_id = int(prefix)
        if overall_only and tracking_id >= 1000:
            continue
        tracking_ids.append(tracking_id)
    return sorted(tracking_ids)


def export_cutoffs_command(args: argparse.Namespace) -> None:
    """从已归档榜线 SQLite ZIP 导出版本化最终线快照。"""

    db_dir = Path(args.db_dir)
    work_dir = Path(args.work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    events = load_json(args.events_masterdata)
    world_blooms = load_json(args.world_blooms_masterdata)
    tracking_ids = (
        parse_event_ids(args.tracking_ids)
        if args.tracking_ids
        else discover_archive_tracking_ids(
            db_dir,
            overall_only=args.overall_only,
        )
    )

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "region": args.region,
        "tracking_ids": tracking_ids,
        "exported": [],
        "skipped": [],
        "failed": [],
    }

    for tracking_id in tracking_ids:
        zip_path = db_dir / f"{tracking_id}_ranking.db.zip"
        try:
            with zipfile.ZipFile(zip_path) as archive:
                bad_member = archive.testzip()
                if bad_member:
                    raise ValueError(f"ZIP CRC校验失败：{bad_member}")

            with tempfile.TemporaryDirectory(
                prefix=f"cutoff-{tracking_id}-",
                dir=work_dir,
            ) as temp_dir:
                database_path = extract_database(
                    zip_path,
                    Path(temp_dir),
                    f"{tracking_id}_ranking.db",
                )
                stats = database_stats(database_path, immutable=True)
                if stats["integrity"].lower() != "ok":
                    raise ValueError(
                        f"integrity_check失败：{stats['integrity']}"
                    )

            source = {
                "kind": "local_sqlite_archive",
                "archive_file": zip_path.name,
                "archive_sha256": sha256_file(zip_path),
                "exported_at": datetime.now(timezone.utc).isoformat(
                    timespec="seconds"
                ),
            }
            snapshot, issue = build_final_snapshot(
                args.region,
                tracking_id,
                stats,
                events,
                world_blooms,
                source,
                args.freshness_seconds,
            )
            if not snapshot:
                report["skipped"].append({
                    "tracking_id": tracking_id,
                    "reason": issue,
                })
                print(f"{tracking_id}: SKIPPED {issue}")
                continue

            path, written = write_snapshot(
                Path(args.final_cutoff_dir),
                snapshot,
                overwrite=args.overwrite,
            )
            item = {
                "tracking_id": tracking_id,
                "path": str(path),
                "written": written,
                "status": snapshot["status"],
                "cutoff_count": len(snapshot["cutoffs"]),
            }
            if written:
                report["exported"].append(item)
                print(
                    f"{tracking_id}: exported={len(snapshot['cutoffs'])} "
                    f"status={snapshot['status']}"
                )
            else:
                report["skipped"].append({
                    "tracking_id": tracking_id,
                    "reason": "目标快照已存在，未覆盖",
                    "path": str(path),
                })
                print(f"{tracking_id}: SKIPPED snapshot exists")
        except Exception as exc:
            report["failed"].append({
                "tracking_id": tracking_id,
                "error": f"{type(exc).__name__}: {exc}",
            })
            print(f"{tracking_id}: FAILED {type(exc).__name__}: {exc}")

    atomic_write_json(work_dir / "export_report.json", report)
    lines = [
        "# 本地 SQLite 历史最终线导出结果",
        "",
        f"生成时间：{report['generated_at']}",
        "",
        f"- 新增：{len(report['exported'])}",
        f"- 跳过：{len(report['skipped'])}",
        f"- 失败：{len(report['failed'])}",
        "",
        "## 跳过项",
        "",
    ]
    lines.extend(
        f"- {item['tracking_id']}：{item['reason']}"
        for item in report["skipped"]
    )
    if report["failed"]:
        lines.extend(["", "## 失败项", ""])
        lines.extend(
            f"- {item['tracking_id']}：{item['error']}"
            for item in report["failed"]
        )
    (work_dir / "EXPORT_ISSUES.md").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )
    if report["failed"]:
        raise SystemExit(1)


def configure_export_parser(parser: argparse.ArgumentParser) -> None:
    """注册本地 SQLite 最终线导出命令参数。"""

    parser.add_argument("--region", default="cn")
    parser.add_argument("--db-dir", required=True)
    parser.add_argument("--tracking-ids")
    parser.add_argument("--overall-only", action="store_true")
    parser.add_argument("--events-masterdata", required=True)
    parser.add_argument("--world-blooms-masterdata", required=True)
    parser.add_argument("--final-cutoff-dir", required=True)
    parser.add_argument("--work-dir", required=True)
    parser.add_argument("--freshness-seconds", type=int, default=600)
    parser.add_argument("--overwrite", action="store_true")
    parser.set_defaults(handler=export_cutoffs_command)


# ================================ 本地预测历史重建 ================================ #

DEFAULT_FORECAST_RANKS = [
    50,
    100,
    200,
    300,
    400,
    500,
    1000,
    2000,
    3000,
    4000,
    5000,
    10000,
]
FORECAST_CSV_COLUMNS = [
    "event_id",
    "to_end_hour",
    "from_start_hour",
    "score",
    "rank",
    "timestamp",
]


def parse_positive_int_list(text: str) -> list[int]:
    """解析逗号分隔的正整数列表并保持首次出现顺序。"""

    values: list[int] = []
    for segment in text.split(","):
        value = int(segment.strip())
        if value <= 0:
            raise ValueError(f"数值必须大于0：{value}")
        if value not in values:
            values.append(value)
    return values


def discover_forecast_event_ids(history_dir: Path) -> list[int]:
    """默认重建已有历史 CSV 对应的活动，避免意外扩大数据范围。"""

    return sorted(
        int(path.stem)
        for path in history_dir.glob("*.csv")
        if path.stem.isdigit()
    )


def load_event_times(masterdata_path: Path) -> dict[int, tuple[int, int]]:
    """读取活动起止时间，时间戳单位统一为秒。"""

    events = load_json(masterdata_path)
    return {
        int(event["id"]): (
            int(event["startAt"]) // 1000,
            int(event["aggregateAt"]) // 1000 + 1,
        )
        for event in events
    }


def prepare_forecast_database_copy(
    db_dir: Path,
    event_id: int,
    work_dir: Path,
) -> Path:
    """
    把数据库准备到任务临时目录。

    压缩归档先解压；未归档数据库连同 WAL 一起复制。所有 SQLite 恢复和
    checkpoint 都只会发生在临时副本，不能修改机器人正在使用的原数据库。
    """

    db_name = f"{event_id}_ranking.db"
    raw_path = db_dir / db_name
    zip_path = db_dir / f"{db_name}.zip"
    copied_path = work_dir / db_name

    if zip_path.exists():
        with zipfile.ZipFile(zip_path) as archive:
            matching_members = [
                member
                for member in archive.namelist()
                if Path(member).name == db_name
            ]
            if len(matching_members) != 1:
                raise ValueError(f"{zip_path} 中未找到唯一的 {db_name}")
            with (
                archive.open(matching_members[0]) as source,
                copied_path.open("wb") as target,
            ):
                shutil.copyfileobj(source, target)
    elif raw_path.exists():
        shutil.copy2(raw_path, copied_path)
    else:
        raise FileNotFoundError(f"缺少 {raw_path} 或 {zip_path}")

    # 部分活动归档后仍残留 WAL。若它和基础 DB 匹配，SQLite 会在临时目录
    # 回放；若不匹配则会报错，避免悄悄输出不一致的训练数据。
    wal_path = db_dir / f"{db_name}-wal"
    if wal_path.exists():
        shutil.copy2(wal_path, work_dir / wal_path.name)
    return copied_path


def query_forecast_rank_samples(
    database_path: Path,
    ranks: Iterable[int],
    sample_interval_seconds: int,
) -> list[tuple[int, int, int]]:
    """按线上相同 SQL 采样，返回 (score, rank, timestamp)。"""

    ranks = list(ranks)
    placeholders = ",".join("?" for _ in ranks)
    sql = f"""
        SELECT score, rank, MIN(ts) AS ts
        FROM ranking
        WHERE rank IN ({placeholders})
        GROUP BY rank, (ts / ?)
        ORDER BY ts ASC
    """
    connection = sqlite3.connect(database_path)
    try:
        rows = connection.execute(
            sql,
            [*ranks, sample_interval_seconds],
        ).fetchall()
    finally:
        connection.close()
    return [
        (int(score), int(rank), int(timestamp))
        for score, rank, timestamp in rows
    ]


def write_forecast_history_csv(
    output_path: Path,
    event_id: int,
    samples: Iterable[tuple[int, int, int]],
    event_start: int,
    event_end: int,
) -> Counter:
    """写出与运行时 `save_rankings_to_csv` 相同字段的 UTF-8 CSV。"""

    rank_counts: Counter = Counter()
    with output_path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=FORECAST_CSV_COLUMNS,
            lineterminator="\n",
        )
        writer.writeheader()
        for score, rank, timestamp in samples:
            rank_counts[rank] += 1
            writer.writerow({
                "event_id": event_id,
                "to_end_hour": (event_end - timestamp) / 3600,
                "from_start_hour": (timestamp - event_start) / 3600,
                "score": score,
                "rank": rank,
                "timestamp": timestamp,
            })
    return rank_counts


def rebuild_forecast_history(args: argparse.Namespace) -> dict[str, Any]:
    """重建目标活动；单个活动失败时停止，不留下半批新旧混合结果。"""

    history_dir = Path(args.history_dir)
    db_dir = Path(args.db_dir)
    temp_root = Path(args.temp_dir)
    backup_dir = Path(args.backup_dir)
    ranks = parse_positive_int_list(args.ranks)
    event_ids = (
        parse_positive_int_list(args.event_ids)
        if args.event_ids
        else discover_forecast_event_ids(history_dir)
    )
    event_times = load_event_times(Path(args.masterdata))

    if not event_ids:
        raise ValueError("没有发现需要重建的历史活动")

    temp_root.mkdir(parents=True, exist_ok=True)
    backup_dir.mkdir(parents=True, exist_ok=True)
    batch_dir = Path(tempfile.mkdtemp(
        prefix="forecast-history-",
        dir=temp_root,
    ))
    generated_dir = batch_dir / "generated"
    generated_dir.mkdir()

    report: dict[str, Any] = {
        "region": args.region,
        "ranks": ranks,
        "sample_interval_seconds": args.sample_interval_minutes * 60,
        "events": [],
    }

    # 先完整生成和校验整批文件，之后才替换现有训练数据。
    for event_id in event_ids:
        if event_id not in event_times:
            raise ValueError(f"MasterData 中不存在活动 {event_id}")
        event_work_dir = batch_dir / f"db-{event_id}"
        event_work_dir.mkdir()
        database_path = prepare_forecast_database_copy(
            db_dir,
            event_id,
            event_work_dir,
        )
        samples = query_forecast_rank_samples(
            database_path,
            ranks,
            args.sample_interval_minutes * 60,
        )
        output_path = generated_dir / f"{event_id}.csv"
        rank_counts = write_forecast_history_csv(
            output_path,
            event_id,
            samples,
            *event_times[event_id],
        )
        missing_ranks = [rank for rank in ranks if rank_counts[rank] == 0]
        if missing_ranks:
            raise ValueError(
                f"活动 {event_id} 缺少档位样本：{missing_ranks}"
            )
        report["events"].append({
            "event_id": event_id,
            "sample_count": sum(rank_counts.values()),
            "rank_counts": {
                str(rank): rank_counts[rank]
                for rank in ranks
            },
        })

    history_dir.mkdir(parents=True, exist_ok=True)
    for event_id in event_ids:
        destination = history_dir / f"{event_id}.csv"
        if destination.exists():
            shutil.copy2(destination, backup_dir / destination.name)
        os.replace(generated_dir / destination.name, destination)

    report["event_count"] = len(event_ids)
    report["total_sample_count"] = sum(
        item["sample_count"]
        for item in report["events"]
    )
    report_path = temp_root / "rebuild_report.json"
    atomic_write_json(report_path, report)
    report["report_path"] = str(report_path)
    report["backup_dir"] = str(backup_dir)
    return report


def configure_rebuild_forecast_parser(
    parser: argparse.ArgumentParser,
) -> None:
    """注册本地预测历史重建命令参数。"""

    parser.add_argument("--region", default="cn")
    parser.add_argument(
        "--ranks",
        default=",".join(map(str, DEFAULT_FORECAST_RANKS)),
    )
    parser.add_argument(
        "--event-ids",
        help="逗号分隔；默认读取已有 history/*.csv",
    )
    parser.add_argument(
        "--sample-interval-minutes",
        type=positive_int_argument,
        default=10,
    )
    parser.add_argument("--db-dir", required=True)
    parser.add_argument("--history-dir", required=True)
    parser.add_argument("--masterdata", required=True)
    parser.add_argument("--temp-dir", required=True)
    parser.add_argument("--backup-dir", required=True)
    parser.set_defaults(handler=rebuild_forecast_command)


def rebuild_forecast_command(args: argparse.Namespace) -> None:
    """执行预测历史重建并打印可审计汇总。"""

    report = rebuild_forecast_history(args)
    print(json.dumps({
        "event_count": report["event_count"],
        "total_sample_count": report["total_sample_count"],
        "report_path": report["report_path"],
        "backup_dir": report["backup_dir"],
    }, ensure_ascii=False))


def positive_int_argument(value: str) -> int:
    """把 CLI 参数限制为正整数，并保留 argparse 的标准错误行为。"""

    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("数值必须大于0")
    return parsed


# ================================ 统一命令入口 ================================ #

def build_parser() -> argparse.ArgumentParser:
    """构造统一 CLI；各离线任务仍保持独立参数和副作用边界。"""

    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    configure_repair_parser(subparsers.add_parser(
        "repair-archives",
        help="安全回放榜线 ZIP 旁遗留 WAL",
    ))
    configure_export_parser(subparsers.add_parser(
        "export-cutoffs",
        help="从本地 SQLite 归档导出最终线",
    ))
    configure_import_parser(subparsers.add_parser(
        "import-cutoffs",
        help="导入外部 JP 历史最终线缓存",
    ))
    configure_rebuild_forecast_parser(subparsers.add_parser(
        "rebuild-forecast",
        help="从 SQLite 归档重建本地预测历史",
    ))
    return parser


def main() -> None:
    """解析子命令并执行对应的离线维护任务。"""

    args = build_parser().parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
