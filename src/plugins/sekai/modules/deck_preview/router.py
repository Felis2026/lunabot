from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterable, Optional

try:
    from ..wl_args import select_world_bloom_turn
except ImportError:
    # 本模块也会被独立测试入口以 `deck_preview.router` 方式加载。
    from wl_args import select_world_bloom_turn


PREVIEW_KEYWORD_PATTERN = re.compile(r"(?i)(?:预览|预演|preview)")


# ================================ 命令模式词与活动ID ================================ #

def extract_preview_keyword(args: str) -> tuple[bool, str]:
    """最先移除一次预览模式词，避免它污染加成、歌曲等后续参数解析。"""

    match = PREVIEW_KEYWORD_PATTERN.search(args)
    if not match:
        return False, re.sub(r"\s+", " ", args).strip()
    remaining = args[:match.start()] + " " + args[match.end():]
    return True, re.sub(r"\s+", " ", remaining).strip()


def extract_explicit_event_id(
    args: str,
    *,
    match_type: str,
) -> tuple[Optional[int], Optional[str]]:
    """只定位显式活动参数，不把歌曲ID或其他数值误当成 fallback 目标。"""

    if match_type in {"full", "all"}:
        match = re.search(r"(?i)(?:活动|event)\s*(\d+)", args)
        if match:
            return int(match.group(1)), match.group(0)
    if match_type in {"simple", "all"}:
        match = re.search(r"(?<!\S)(\d{1,3})(?!\S)", args)
        if match:
            return int(match.group(1)), match.group(0)
    return None, None


def build_preview_guide(
    event_id: int,
    *,
    explicit_cn: bool,
    command_kind: str = "event",
) -> str:
    """生成已经与用户确认的固定引导文案。"""

    prefix = "/cn" if explicit_cn else "/"
    command_args = (
        f"加成组卡 event{event_id} 预览 120"
        if command_kind == "bonus"
        else f"活动组卡 {event_id} 预览"
    )
    return (
        f"国服活动 #{event_id} 数据尚未上线。\n"
        "如需按日服活动规则、使用你的国服 Suite 预演，请发送：\n"
        f"{prefix}{command_args}"
    )


def format_preview_result_title(original_title: str) -> str:
    """保留原组卡标题，只在末尾追加统一的 Preview 标记。"""

    return f"{original_title}（预览）"


# ================================ WL章节与轮次 ================================ #

def choose_world_bloom_chapter(
    chapters: Iterable[dict[str, Any]],
    *,
    chapter_no: int | None,
    character_id: int | None,
) -> dict[str, Any]:
    """
    选择 preview WL 章节。

    未指定时始终选 `chapterNo` 最小的一章，绝不根据已经结束的 JP 时间戳选末章。
    """

    candidates = sorted(
        chapters,
        key=lambda item: (
            item.get("chapterNo", 10**9),
            item.get("chapterStartAt", 10**18),
        ),
    )
    if not candidates:
        raise ValueError("该 WL 活动没有已校验章节")
    if chapter_no is not None:
        chapter = next(
            (item for item in candidates if item.get("chapterNo") == chapter_no),
            None,
        )
        if chapter is None:
            raise ValueError(f"该 WL 活动没有章节 {chapter_no}")
        return chapter
    if character_id is not None:
        chapter = next(
            (
                item
                for item in candidates
                if item.get("gameCharacterId") == character_id
            ),
            None,
        )
        if chapter is None:
            raise ValueError(f"该 WL 活动没有角色 {character_id} 的章节")
        return chapter
    return candidates[0]


# ================================ 正式切回持久化 ================================ #

class OfficialActivationStore:
    """事件级单向切回记录；必须先原子落盘，再更新进程内视图。"""

    def __init__(self, path: str):
        self.path = Path(path)
        self.lock_path = self.path.with_name(f".{self.path.name}.lock")
        self._lock = threading.Lock()
        self._events: dict[str, dict[str, Any]] = {}
        self._detected_events: dict[str, dict[str, Any]] = {}
        self._healthy = True
        self._loaded_signature: tuple[int, int, int, int] | None = None
        self._load()

    def _disk_signature(self) -> tuple[int, int, int, int] | None:
        """同时使用 inode、大小和时间识别原子替换，规避同纳秒 mtime 的漏刷新。"""

        try:
            stat = self.path.stat()
        except FileNotFoundError:
            return None
        return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)

    def _read_disk_state(
        self,
    ) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}, {}
        if not isinstance(data, dict):
            raise ValueError("正式切回状态顶层必须是对象")
        if data.get("schema_version", 1) != 1:
            raise ValueError("正式切回状态版本不受支持")
        events = data.get("events", {})
        detected_events = data.get("detected_events", {})
        if not isinstance(events, dict) or not isinstance(detected_events, dict):
            raise ValueError("正式切回状态字段必须是对象")

        def normalize(
            values: dict[str, Any],
            label: str,
        ) -> dict[str, dict[str, Any]]:
            normalized: dict[str, dict[str, Any]] = {}
            for raw_event_id, entry in values.items():
                event_id = str(raw_event_id)
                if (
                    not event_id.isdigit()
                    or int(event_id) <= 0
                    or not isinstance(entry, dict)
                    or (
                        entry.get("event_id") is not None
                        and entry.get("event_id") != int(event_id)
                    )
                ):
                    raise ValueError(f"{label} 存在非法活动记录")
                normalized[event_id] = entry
            return normalized

        return normalize(events, "events"), normalize(
            detected_events,
            "detected_events",
        )

    def _load(self) -> None:
        try:
            self._events, self._detected_events = self._read_disk_state()
            self._loaded_signature = self._disk_signature()
        except Exception:
            # 损坏记录不能被当作“没有激活记录”，上层会令整个 preview 失败关闭。
            self._events = {}
            self._detected_events = {}
            self._healthy = False

    def _refresh_if_changed(self) -> None:
        """其他 Bot 进程原子更新状态后，在下一次路由判断前刷新内存。"""

        if not self._healthy:
            raise RuntimeError("正式切回状态已损坏，路由失败关闭")
        current_signature = self._disk_signature()
        if current_signature == self._loaded_signature:
            return
        with self._lock:
            current_signature = self._disk_signature()
            if current_signature == self._loaded_signature:
                return
            if current_signature is None and self._loaded_signature is not None:
                self._healthy = False
                raise RuntimeError("正式切回状态文件异常消失，路由失败关闭")
            try:
                self._events, self._detected_events = self._read_disk_state()
                self._loaded_signature = current_signature
            except Exception as exc:
                self._healthy = False
                raise RuntimeError("正式切回状态已损坏，路由失败关闭") from exc

    def _acquire_process_lock(self, timeout_seconds: float = 2.0) -> None:
        """用短时独占文件锁保护多 Bot 进程的读改写，避免相互覆盖状态。"""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                fd = os.open(
                    self.lock_path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                )
            except FileExistsError:
                try:
                    if time.time() - self.lock_path.stat().st_mtime > 30:
                        self.lock_path.unlink()
                        continue
                except FileNotFoundError:
                    continue
                if time.monotonic() >= deadline:
                    raise RuntimeError("正式切回状态文件正被其他进程更新")
                time.sleep(0.02)
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                file.write(f"pid={os.getpid()}\n")
            return

    def _release_process_lock(self) -> None:
        try:
            self.lock_path.unlink()
        except FileNotFoundError:
            pass

    def _refresh_for_write(
        self,
    ) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
        """锁内重新读取磁盘，保证跨进程更新基于最新状态。"""

        try:
            events, detected_events = self._read_disk_state()
        except Exception as exc:
            self._healthy = False
            raise RuntimeError("正式切回状态已损坏，拒绝更新") from exc
        self._events = events
        self._detected_events = detected_events
        self._loaded_signature = self._disk_signature()
        return events, detected_events

    def healthy(self) -> bool:
        try:
            self._refresh_if_changed()
        except RuntimeError:
            return False
        return self._healthy

    def is_active(self, event_id: int) -> bool:
        self._refresh_if_changed()
        return str(event_id) in self._events

    def is_detected(self, event_id: int) -> bool:
        self._refresh_if_changed()
        return str(event_id) in self._detected_events

    def get(self, event_id: int) -> dict[str, Any] | None:
        self._refresh_if_changed()
        entry = self._events.get(str(event_id))
        return dict(entry) if entry else None

    def detected_ids(self) -> set[int]:
        self._refresh_if_changed()
        return {
            int(event_id)
            for event_id in self._detected_events
            if event_id.isdigit()
        }

    def all(self) -> dict[str, dict[str, Any]]:
        self._refresh_if_changed()
        return {
            event_id: dict(entry)
            for event_id, entry in self._events.items()
        }

    def all_detected(self) -> dict[str, dict[str, Any]]:
        """返回全部 CN_DETECTED 记录的只读副本。"""

        self._refresh_if_changed()
        return {
            event_id: dict(entry)
            for event_id, entry in self._detected_events.items()
        }

    def _write_state(
        self,
        events: dict[str, dict[str, Any]],
        detected_events: dict[str, dict[str, Any]],
    ) -> None:
        """原子写入检测与激活状态；调用方只在成功后更新内存视图。"""

        payload = {
            "schema_version": 1,
            "events": events,
            "detected_events": detected_events,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_name(
            f".{self.path.name}.{uuid.uuid4().hex}.tmp"
        )
        with temp_path.open("w", encoding="utf-8", newline="\n") as file:
            json.dump(
                payload,
                file,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_path, self.path)
        self._loaded_signature = self._disk_signature()

    def mark_detected(self, event_id: int, entry: dict[str, Any]) -> bool:
        """持久化单向 CN_DETECTED；活动正式激活前绝不再允许 JP fallback。"""

        key = str(event_id)
        with self._lock:
            self._acquire_process_lock()
            try:
                events, detected_events = self._refresh_for_write()
                if key in events or key in detected_events:
                    return False
                next_detected = dict(detected_events)
                next_detected[key] = {
                    **entry,
                    "event_id": event_id,
                    "detected_at": int(entry.get("detected_at") or time.time()),
                }
                self._write_state(events, next_detected)
                self._detected_events = next_detected
                return True
            finally:
                self._release_process_lock()

    def activate(self, event_id: int, entry: dict[str, Any]) -> bool:
        """返回是否新增；已有记录永不被自动删除或降级。"""

        key = str(event_id)
        with self._lock:
            self._acquire_process_lock()
            try:
                events, detected_events = self._refresh_for_write()
                if key in events:
                    return False
                next_events = dict(events)
                next_events[key] = {
                    **entry,
                    "event_id": event_id,
                    "activated_at": int(entry.get("activated_at") or time.time()),
                }
                next_detected = dict(detected_events)
                next_detected.pop(key, None)
                self._write_state(next_events, next_detected)
                # 只有 os.replace 成功后才切进程内路由状态。
                self._events = next_events
                self._detected_events = next_detected
                return True
            finally:
                self._release_process_lock()

    def reset(self, event_id: int) -> bool:
        """显式清除一个活动的检测/激活记录，供维护者重新执行正式切回核验。"""

        key = str(event_id)
        with self._lock:
            self._acquire_process_lock()
            try:
                events, detected_events = self._refresh_for_write()
                if key not in events and key not in detected_events:
                    return False
                next_events = dict(events)
                next_detected = dict(detected_events)
                next_events.pop(key, None)
                next_detected.pop(key, None)
                self._write_state(next_events, next_detected)
                self._events = next_events
                self._detected_events = next_detected
                return True
            finally:
                self._release_process_lock()


# ================================ 维护命令行 ================================ #

def _run_activation_admin_cli() -> int:
    """提供只读审计和带确认参数的单活动状态重置。"""

    import argparse

    parser = argparse.ArgumentParser(
        description="查看或重置 CN 预览正式切回状态",
    )
    parser.add_argument(
        "--state-file",
        default=(
            "data/sekai/deckrec/rulesets/"
            "cn_jp_preview_v1/official_active.json"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status")
    reset_parser = subparsers.add_parser("reset")
    reset_parser.add_argument("event_id", type=int)
    reset_parser.add_argument(
        "--confirm",
        action="store_true",
        help="确认清除该活动的 CN_DETECTED/OFFICIAL_ACTIVE 记录",
    )
    args = parser.parse_args()

    store = OfficialActivationStore(args.state_file)
    if not store.healthy():
        raise SystemExit("状态文件损坏；拒绝把损坏状态当作空记录处理")
    if args.command == "status":
        print(json.dumps(
            {
                "active": store.all(),
                "detected": store.all_detected(),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ))
        return 0
    if not args.confirm:
        raise SystemExit("重置属于显式维护操作，请追加 --confirm")
    changed = store.reset(args.event_id)
    print(
        f"event={args.event_id} "
        + ("状态已清除" if changed else "没有检测或激活记录")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_run_activation_admin_cli())
