from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Any, Callable
import inspect


# ================================ 运行时控制面注册表 ================================ #
# 这里记录群开关与周期任务的运行时注册信息，供业务模块和中控台共同查看。
# 它属于共享基础设施，不是只给中控台用的临时拼接文件。
_INTERNAL_FILE_SUFFIXES = (
    'src/plugins/utils/control_plane.py',
    'src/plugins/utils/handler.py',
    'src/plugins/utils/utils.py',
)


def _normalize_path(value: str | None) -> str:
    if not value:
        return ''
    return Path(value).as_posix()


def _format_time(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat(timespec='seconds')


def _normalize_group_ids(group_ids: Any) -> list[int]:
    result: list[int] = []
    if not isinstance(group_ids, list):
        return result
    for item in group_ids:
        try:
            result.append(int(item))
        except Exception:
            continue
    return sorted(set(result))


def _safe_call(func: Callable | None, *args, default=None, **kwargs):
    if func is None:
        return default
    try:
        return func(*args, **kwargs)
    except Exception:
        return default


# ================================ 快照数据模型 ================================ #
@dataclass
class RegistrationOrigin:
    module: str = ''
    file_path: str = ''
    qualname: str = ''

    def snapshot(self) -> dict[str, str]:
        return {
            'module': self.module,
            'file_path': self.file_path,
            'qualname': self.qualname,
        }


@dataclass
class GroupToggleRegistration:
    key: str
    name: str
    mode: str
    is_service: bool
    db_key: str
    origin: RegistrationOrigin = field(default_factory=RegistrationOrigin)
    consumer_origins: list[RegistrationOrigin] = field(default_factory=list)
    toggle_ref: Any = None
    list_getter: Callable[[], list[int]] | None = None
    checker: Callable[[int], bool] | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    registered_at: datetime = field(default_factory=datetime.now)

    def snapshot(self, *, group_id: int | None = None) -> dict[str, Any]:
        enabled_group_ids = _normalize_group_ids(_safe_call(self.list_getter, default=[]))
        result = {
            'key': self.key,
            'name': self.name,
            'mode': self.mode,
            'is_service': self.is_service,
            'db_key': self.db_key,
            'registered_at': _format_time(self.registered_at),
            'origin': self.origin.snapshot(),
            'consumer_origins': [item.snapshot() for item in self.consumer_origins],
            'default_enabled': self.mode == 'blacklist',
            'enabled_group_ids': enabled_group_ids,
            'enabled_group_count': len(enabled_group_ids),
            'description': str(self.meta.get('description', '')),
            'meta': dict(self.meta),
        }
        if group_id is not None:
            result['group_id'] = int(group_id)
            result['enabled'] = bool(_safe_call(self.checker, int(group_id), default=False))
        return result


@dataclass
class PeriodicTaskRegistration:
    key: str
    name: str
    interval_desc: str
    origin: RegistrationOrigin = field(default_factory=RegistrationOrigin)
    every_output: bool = False
    error_output: bool = True
    error_limit: int = 5
    delay: float | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    registered_at: datetime = field(default_factory=datetime.now)
    status: str = 'pending'
    started_at: datetime | None = None
    last_run_started_at: datetime | None = None
    last_success_at: datetime | None = None
    last_error_at: datetime | None = None
    last_error: str | None = None
    total_runs: int = 0
    total_failures: int = 0
    consecutive_errors: int = 0
    next_run_at: datetime | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            'key': self.key,
            'name': self.name,
            'interval_desc': self.interval_desc,
            'origin': self.origin.snapshot(),
            'every_output': self.every_output,
            'error_output': self.error_output,
            'error_limit': self.error_limit,
            'delay': self.delay,
            'meta': dict(self.meta),
            'registered_at': _format_time(self.registered_at),
            'status': self.status,
            'started_at': _format_time(self.started_at),
            'last_run_started_at': _format_time(self.last_run_started_at),
            'last_success_at': _format_time(self.last_success_at),
            'last_error_at': _format_time(self.last_error_at),
            'last_error': self.last_error,
            'total_runs': self.total_runs,
            'total_failures': self.total_failures,
            'consecutive_errors': self.consecutive_errors,
            'next_run_at': _format_time(self.next_run_at),
        }


_group_toggle_lock = RLock()
_periodic_task_lock = RLock()
_group_toggles: dict[str, GroupToggleRegistration] = {}
_periodic_tasks: dict[str, PeriodicTaskRegistration] = {}


def _is_internal_file(file_path: str) -> bool:
    normalized = _normalize_path(file_path)
    return any(normalized.endswith(suffix) for suffix in _INTERNAL_FILE_SUFFIXES)


# ================================ 注册来源识别 ================================ #
# 向上回溯调用栈，直到离开 control_plane 内部实现，这样登记下来的才是真正的业务注册来源。
def infer_registration_origin() -> RegistrationOrigin:
    frame = inspect.currentframe()
    if frame is not None:
        frame = frame.f_back
    while frame is not None:
        file_path = _normalize_path(frame.f_code.co_filename)
        if not _is_internal_file(file_path):
            return RegistrationOrigin(
                module=str(frame.f_globals.get('__name__', '')),
                file_path=file_path,
                qualname=str(frame.f_code.co_name),
            )
        frame = frame.f_back
    return RegistrationOrigin()


def _task_origin_from_func(func: Callable) -> RegistrationOrigin:
    file_path = ''
    try:
        file_path = _normalize_path(inspect.getsourcefile(func) or '')
    except Exception:
        file_path = ''
    return RegistrationOrigin(
        module=str(getattr(func, '__module__', '')),
        file_path=file_path,
        qualname=str(getattr(func, '__qualname__', getattr(func, '__name__', ''))),
    )


def _build_group_toggle_key(name: str, mode: str) -> str:
    return f'{mode}:{name}'


def _describe_interval(interval: Any) -> str:
    config = getattr(interval, 'config', None)
    keys = getattr(interval, 'keys', None)
    if config is not None and keys is not None:
        key = '.'.join(str(item) for item in keys)
        config_name = getattr(config, 'name', 'config')
        return f'ConfigItem({config_name}:{key})'
    return repr(interval)

# ================================ 群开关注册表 ================================ #
# 注册或刷新单个群开关；重载时重复注册是预期行为，因此这里选择原地更新。
def register_group_toggle(
    toggle_ref: Any,
    *,
    name: str,
    mode: str,
    is_service: bool,
    db_key: str,
    meta: dict[str, Any] | None = None,
) -> GroupToggleRegistration:
    key = _build_group_toggle_key(name, mode)
    origin = infer_registration_origin()
    with _group_toggle_lock:
        entry = _group_toggles.get(key)
        if entry is None:
            entry = GroupToggleRegistration(
                key=key,
                name=name,
                mode=mode,
                is_service=is_service,
                db_key=db_key,
                origin=origin,
                toggle_ref=toggle_ref,
                list_getter=getattr(toggle_ref, 'get', None),
                checker=getattr(toggle_ref, 'check_id', None),
                meta=dict(meta or {}),
            )
            _group_toggles[key] = entry
            return entry

        entry.is_service = is_service
        entry.db_key = db_key
        entry.toggle_ref = toggle_ref
        entry.list_getter = getattr(toggle_ref, 'get', None)
        entry.checker = getattr(toggle_ref, 'check_id', None)
        entry.meta.update(meta or {})
        origin_snapshot = origin.snapshot()
        known_origins = [item.snapshot() for item in [entry.origin, *entry.consumer_origins]]
        if origin_snapshot not in known_origins:
            entry.consumer_origins.append(origin)
        return entry


def list_group_toggles(*, include_non_service: bool = True) -> list[dict[str, Any]]:
    with _group_toggle_lock:
        entries = list(_group_toggles.values())
    snapshots = []
    for entry in entries:
        if not include_non_service and not entry.is_service:
            continue
        snapshots.append(entry.snapshot())
    snapshots.sort(key=lambda item: (not item['is_service'], item['mode'], item['name']))
    return snapshots


def get_group_toggle_registration(name_or_key: str, *, raise_exc: bool = False) -> GroupToggleRegistration | None:
    with _group_toggle_lock:
        if name_or_key in _group_toggles:
            return _group_toggles[name_or_key]
        matches = [item for item in _group_toggles.values() if item.name == name_or_key]
    if len(matches) == 1:
        return matches[0]
    if raise_exc:
        if len(matches) > 1:
            keys = ', '.join(item.key for item in matches)
            raise KeyError(f'群开关 {name_or_key} 存在多个注册项: {keys}')
        raise KeyError(f'未找到群开关 {name_or_key}')
    return None


def get_group_service_matrix(group_id: int, *, include_non_service: bool = True) -> list[dict[str, Any]]:
    group_id = int(group_id)
    result = []
    for entry in list_group_toggles(include_non_service=include_non_service):
        result.append({
            **entry,
            'group_id': group_id,
            'enabled': group_id in entry['enabled_group_ids'] if entry['mode'] == 'whitelist' else group_id not in entry['enabled_group_ids'],
        })
    return result


# ================================ 周期任务注册表 ================================ #
# 将调度器可见的任务元信息单独登记出来，便于中控台读取运行状态，而不用侵入任务执行逻辑。
def register_periodic_task(
    func: Callable,
    *,
    interval: Any,
    name: str,
    every_output: bool = False,
    error_output: bool = True,
    error_limit: int = 5,
    delay: float | None = None,
    meta: dict[str, Any] | None = None,
) -> str:
    origin = _task_origin_from_func(func)
    key = f'{origin.module}:{origin.qualname}:{name}'
    with _periodic_task_lock:
        entry = _periodic_tasks.get(key)
        if entry is None:
            entry = PeriodicTaskRegistration(
                key=key,
                name=name,
                interval_desc=_describe_interval(interval),
                origin=origin,
                every_output=every_output,
                error_output=error_output,
                error_limit=error_limit,
                delay=delay,
                meta=dict(meta or {}),
            )
            _periodic_tasks[key] = entry
        else:
            entry.interval_desc = _describe_interval(interval)
            entry.every_output = every_output
            entry.error_output = error_output
            entry.error_limit = error_limit
            entry.delay = delay
            entry.meta.update(meta or {})
        return key


def mark_periodic_task_loop_started(task_key: str, *, started_at: datetime | None = None) -> None:
    with _periodic_task_lock:
        entry = _periodic_tasks.get(task_key)
        if entry is None:
            return
        now = started_at or datetime.now()
        entry.status = 'running'
        entry.started_at = now


def mark_periodic_task_run_started(
    task_key: str,
    *,
    started_at: datetime | None = None,
    next_run_at: datetime | None = None,
) -> None:
    with _periodic_task_lock:
        entry = _periodic_tasks.get(task_key)
        if entry is None:
            return
        now = started_at or datetime.now()
        entry.status = 'running'
        entry.last_run_started_at = now
        entry.next_run_at = next_run_at
        entry.total_runs += 1


def mark_periodic_task_run_succeeded(task_key: str, *, finished_at: datetime | None = None) -> None:
    with _periodic_task_lock:
        entry = _periodic_tasks.get(task_key)
        if entry is None:
            return
        now = finished_at or datetime.now()
        entry.status = 'running'
        entry.last_success_at = now
        entry.last_error = None
        entry.consecutive_errors = 0


def mark_periodic_task_run_failed(task_key: str, exc: Exception, *, failed_at: datetime | None = None) -> None:
    with _periodic_task_lock:
        entry = _periodic_tasks.get(task_key)
        if entry is None:
            return
        now = failed_at or datetime.now()
        entry.status = 'error'
        entry.last_error_at = now
        entry.last_error = str(exc)
        entry.total_failures += 1
        entry.consecutive_errors += 1


def mark_periodic_task_cancelled(task_key: str) -> None:
    with _periodic_task_lock:
        entry = _periodic_tasks.get(task_key)
        if entry is None:
            return
        entry.status = 'cancelled'


def list_periodic_tasks() -> list[dict[str, Any]]:
    with _periodic_task_lock:
        entries = list(_periodic_tasks.values())
    snapshots = [entry.snapshot() for entry in entries]
    snapshots.sort(key=lambda item: item['name'])
    return snapshots
