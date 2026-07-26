from ..utils import *
from ..llm import ChatSession, get_model_preset, ChatSessionResponse
from apscheduler.triggers.cron import CronTrigger
from nonebot.adapters.onebot.v11 import Message, MessageSegment


config = Config('cron.cron')
logger = get_logger('Cron')
file_db = get_file_db('data/cron/cron.json', logger)
cd = ColdDown(file_db, logger)


def _flush_cron_db_after_toggle(_group_id: int):
    """立即保存群开关，避免容器异常退出时恢复到旧状态。"""
    file_db.save()


async def _resume_pending_one_time_tasks(group_id: int):
    """开启本群 Cron 时，仅恢复仍处于补发宽限期的一次性任务。"""
    file_db.save()
    for task in file_db.get(f"tasks_{group_id}", []):
        if task.get('mute') or not _should_preserve_pending_one_time_task(task):
            continue
        if scheduler.get_job(_get_task_job_id(group_id, task['id'])) is not None:
            continue
        try:
            await add_cron_job(task, verbose=True)
        except Exception as exc:
            logger.print_exc(
                f"恢复群组 {group_id} 的一次性任务 {task['id']} 失败: {exc}"
            )


gbl = get_group_black_list(
    file_db,
    logger,
    'cron',
    on_func=_resume_pending_one_time_tasks,
    off_func=_flush_cron_db_after_toggle,
    allow_group_admin_current_group=True,
)


# ================================ 调度参数校验 ================================ #

ALLOWED_CRON_PARAMETER_KEYS = {
    'year',
    'month',
    'day',
    'week',
    'day_of_week',
    'hour',
    'minute',
    'second',
    'start_date',
    'end_date',
}


class CronTaskValidationError(Exception):
    """表示用户提醒可以被理解，但生成的调度参数不符合安全约束。"""


def _scheduler_now() -> datetime:
    """使用调度器时区生成当前时间，避免依赖容器隐式时区。"""
    return datetime.now(scheduler.timezone)


def _parse_task_datetime(value: str | None) -> datetime | None:
    """解析任务中持久化的 ISO 时间，并统一到调度器时区。"""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        logger.warning(f"忽略无法解析的 Cron 时间: {value!r}")
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=scheduler.timezone)
    return parsed.astimezone(scheduler.timezone)


def _normalize_exact_time_window(parameters: dict) -> dict:
    """把 start_date == end_date 的唯一时刻修正为精确的一次性 Cron 参数。"""
    normalized_parameters = dict(parameters)
    if 'start_date' not in normalized_parameters or 'end_date' not in normalized_parameters:
        return normalized_parameters

    start_time = _parse_task_datetime(normalized_parameters.get('start_date'))
    end_time = _parse_task_datetime(normalized_parameters.get('end_date'))
    if start_time is None or end_time is None or start_time != end_time:
        return normalized_parameters

    # LLM 偶尔会把“半小时后”误写为 minute=*/30，再附加零长度时间窗。
    # 零长度时间窗只可能表示一个确定时刻，因此以该时刻为准可无损恢复用户意图。
    logger.info(
        f"将零长度 Cron 时间窗修正为精确一次性任务: {start_time.isoformat()}"
    )
    return {
        'year': str(start_time.year),
        'month': str(start_time.month),
        'day': str(start_time.day),
        'hour': str(start_time.hour),
        'minute': str(start_time.minute),
        'second': str(start_time.second),
    }


def _analyze_task_schedule(parameters: dict, enforce_min_interval: bool = True):
    """校验 CronTrigger 参数，并返回归一化参数及前两次触发时间。"""
    if not isinstance(parameters, dict) or not parameters:
        raise CronTaskValidationError("没有生成有效的提醒时间，请换一种说法后重试")

    parameters = _normalize_exact_time_window(parameters)
    unknown_keys = set(parameters) - ALLOWED_CRON_PARAMETER_KEYS
    if unknown_keys:
        logger.warning(f"Cron参数包含未允许字段: {sorted(unknown_keys)}")
        raise CronTaskValidationError("生成的提醒参数不受支持，请换一种说法后重试")

    normalized_parameters = {
        key: str(value)
        for key, value in parameters.items()
    }
    try:
        trigger = CronTrigger(
            timezone=scheduler.timezone,
            **normalized_parameters,
        )
        first_run_time = trigger.get_next_fire_time(None, _scheduler_now())
    except Exception as exc:
        logger.warning(f"Cron参数校验失败: {exc}")
        raise CronTaskValidationError("提醒时间格式无效，请换一种说法后重试") from exc

    if first_run_time is None:
        raise CronTaskValidationError("没有可执行的未来时间，请重新指定提醒时间")

    second_run_time = None
    previous_run_time = first_run_time
    min_interval_seconds = int(config.get('min_repeat_interval_seconds', 60))

    # 采样后续触发时间，防止“每分钟的第0秒和第30秒”一类表达绕过最短间隔限制。
    for _ in range(64):
        next_run_time = trigger.get_next_fire_time(previous_run_time, previous_run_time)
        if next_run_time is None:
            break
        if second_run_time is None:
            second_run_time = next_run_time
        if (
            enforce_min_interval
            and (next_run_time - previous_run_time).total_seconds() < min_interval_seconds
        ):
            raise CronTaskValidationError(
                f"重复提醒的最短间隔为 {min_interval_seconds} 秒，请调整后重试"
            )
        previous_run_time = next_run_time

    return normalized_parameters, first_run_time, second_run_time


def _apply_schedule_metadata(task: dict, enforce_min_interval: bool = True):
    """为新任务补充兼容性元数据；旧任务仍可按原字段继续加载。"""
    parameters, first_run_time, second_run_time = _analyze_task_schedule(
        task.get('parameters'),
        enforce_min_interval=enforce_min_interval,
    )
    task['parameters'] = parameters
    task['schedule_type'] = 'one_time' if second_run_time is None else 'recurring'
    if second_run_time is None:
        task['scheduled_at'] = first_run_time.isoformat()
    else:
        task.pop('scheduled_at', None)


# ================================ 文本与状态兼容 ================================ #

def _escape_cq_text(value) -> str:
    """把不可信文本序列化为纯文本，避免其中的 CQ 码被 OneBot 解析。"""
    return str(MessageSegment.text(str(value)))


def _render_task_content(content: str, current_time: datetime, count: int) -> str:
    """仅替换受支持占位符，其他花括号按普通文本保留。"""
    left_brace_token = "\x00CRON_LEFT_BRACE\x00"
    right_brace_token = "\x00CRON_RIGHT_BRACE\x00"
    rendered = str(content)
    rendered = rendered.replace('{{', left_brace_token).replace('}}', right_brace_token)
    rendered = rendered.replace('{time}', current_time.strftime('%Y-%m-%d %H:%M:%S'))
    rendered = rendered.replace('{count}', str(count))
    return rendered.replace(left_brace_token, '{').replace(right_brace_token, '}')


def _is_one_time_task(task: dict) -> bool:
    return task.get('schedule_type') == 'one_time'


def _is_completed_one_time_task(task: dict) -> bool:
    return _is_one_time_task(task) and bool(task.get('completed_at'))


def _should_preserve_pending_one_time_task(task: dict) -> bool:
    """在补发宽限期内保留未完成的一次性任务，避免清理器提前删除。"""
    if not _is_one_time_task(task) or _is_completed_one_time_task(task):
        return False
    scheduled_at = _parse_task_datetime(task.get('scheduled_at'))
    if scheduled_at is None:
        return False
    grace_seconds = int(config.get('one_time_misfire_grace_seconds', 300))
    return _scheduler_now() <= scheduled_at + timedelta(seconds=grace_seconds)


def _get_task_job_id(group_id: int, task_id: int) -> str:
    return f"{group_id}_{task_id}"


# ================================ 创建额度控制 ================================ #

CRON_CREATE_HISTORY_KEY = 'create_history'


def _iter_active_tasks():
    """遍历尚未完成或清理的任务；静音和群关闭状态仍占用任务额度。"""
    for key in file_db.keys():
        if not key.startswith('tasks_'):
            continue
        for task in file_db.get(key, []):
            if not _is_completed_one_time_task(task):
                yield task


def _prune_create_history(history: dict, now_timestamp: float) -> dict:
    """清理过期或损坏的创建记录，避免限额历史无限增长。"""
    window_seconds = int(config.get('create_rate_limit_window_seconds', 600))
    pruned_history = {}
    for user_id, timestamps in history.items():
        recent_timestamps = []
        for timestamp in timestamps if isinstance(timestamps, list) else []:
            try:
                parsed_timestamp = float(timestamp)
            except (TypeError, ValueError):
                continue
            if now_timestamp - parsed_timestamp < window_seconds:
                recent_timestamps.append(parsed_timestamp)
        if recent_timestamps:
            pruned_history[str(user_id)] = recent_timestamps
    return pruned_history


def _assert_task_creation_limits(group_id: int, user_id: int):
    """在调用 LLM 前及写库前检查用户、群聊和时间窗口额度。"""
    active_tasks = list(_iter_active_tasks())
    max_user_tasks = int(config.get('max_active_tasks_per_user', 5))
    user_task_count = sum(
        str(task.get('user_id')) == str(user_id)
        for task in active_tasks
    )
    if user_task_count >= max_user_tasks:
        raise ReplyException(
            f"你当前已有 {user_task_count} 个活跃提醒，最多只能保留 {max_user_tasks} 个；"
            "请先删除或等待现有提醒结束"
        )

    max_group_tasks = int(config.get('max_active_tasks_per_group', 30))
    group_task_count = sum(
        int(task.get('group_id', 0)) == int(group_id)
        for task in active_tasks
    )
    if group_task_count >= max_group_tasks:
        raise ReplyException(
            f"本群当前已有 {group_task_count} 个活跃提醒，已达到 {max_group_tasks} 个上限"
        )

    now_timestamp = _scheduler_now().timestamp()
    history = _prune_create_history(
        file_db.get(CRON_CREATE_HISTORY_KEY, {}),
        now_timestamp,
    )
    recent_creations = history.get(str(user_id), [])
    max_creations = int(config.get('max_creations_per_window', 3))
    if len(recent_creations) >= max_creations:
        window_seconds = int(config.get('create_rate_limit_window_seconds', 600))
        retry_after_seconds = max(
            1,
            math.ceil(recent_creations[0] + window_seconds - now_timestamp),
        )
        retry_after_minutes = max(1, math.ceil(retry_after_seconds / 60))
        raise ReplyException(
            f"你在最近 {math.ceil(window_seconds / 60)} 分钟内已创建 {max_creations} 个提醒，"
            f"请约 {retry_after_minutes} 分钟后再试"
        )


def _record_task_creation(user_id: int):
    """成功注册任务后记录创建时间，并立即持久化限额状态。"""
    now_timestamp = _scheduler_now().timestamp()
    history = _prune_create_history(
        file_db.get(CRON_CREATE_HISTORY_KEY, {}),
        now_timestamp,
    )
    history.setdefault(str(user_id), []).append(now_timestamp)
    file_db.set(CRON_CREATE_HISTORY_KEY, history)
    file_db.save()

# 获取下次提醒时间描述
def get_task_next_run_time_str(group_id, task_id):
    task_job = scheduler.get_job(_get_task_job_id(group_id, task_id))
    if task_job is None:
        return "无下次提醒"
    if task_job.next_run_time is None:
        return "无下次提醒"
    return f"下次: {task_job.next_run_time.strftime('%Y-%m-%d %H:%M:%S')}"

# 获取时间描述
def get_task_time_desc(task):
    param = task['parameters']
    desc = ""
    desc += param.get('year', "*") + " "
    desc += param.get('month', "*") + " "
    desc += param.get('day', "*") + " "
    desc += param.get('hour', "*") + " "
    desc += param.get('minute', "*") + " "
    desc += param.get('second', "*")
    if 'week'           in param: desc += f" w={param['week']}"
    if 'day_of_week'    in param: desc += f" dow={param['day_of_week']}"
    if 'start_date'     in param: desc += f" s={param['start_date']}"
    if 'end_date'       in param: desc += f" t={param['end_date']}"
    return desc

# 获取task描述字符串
def task_to_str(task):
    res = f"【{task['id']}】{'(muted) ' if task['mute'] else ''}\n"
    res += f"创建者: {task['user_id']} 订阅者: {len(task['sub_users'])}人\n"
    res += f"内容: {truncate(task['content'], 64)}\n"
    res += f"时间: {get_task_time_desc(task)}\n"
    res += f"{get_task_next_run_time_str(task['group_id'], task['id'])}\n"
    return _escape_cq_text(res)

# 根据ctx查找task
async def find_task(
    ctx: HandlerContext,
    check_permission: bool = False,
    allow_group_admin: bool = False,
    raise_exc: bool = True,
):
    """查找本群任务，并按操作类型校验创建者或群管理权限。"""
    try: task_id = int(ctx.get_args().strip().split()[0])
    except: raise ReplyException("请在命令后输入任务ID")
    group_tasks = file_db.get(f"tasks_{ctx.group_id}", [])
    for task in group_tasks:
        if task['id'] == task_id:
            if check_permission:
                is_creator = str(task['user_id']) == str(ctx.user_id)
                permitted = check_superuser(ctx.event) or is_creator
                if not permitted and allow_group_admin:
                    permitted = await check_group_admin_or_superuser(
                        ctx.bot,
                        ctx.event,
                        int(ctx.group_id),
                    )
                if not permitted:
                    if allow_group_admin:
                        raise ReplyException(
                            "只有任务创建者、群主、群管理员或 superuser 才能执行该操作"
                        )
                    raise ReplyException("只有任务创建者或 superuser 才能执行该操作")
            return deepcopy(task)
    if raise_exc:
        raise ReplyException(f"任务【{task_id}】不存在")
    return None

# 更新task
def update_task(task, flush: bool = False):
    """更新任务；用户主动修改和一次性完成状态可要求立即落盘。"""
    group_tasks = file_db.get(f"tasks_{task['group_id']}", [])
    for i in range(len(group_tasks)):
        if group_tasks[i]['id'] == task['id']:
            group_tasks[i] = task
            file_db.set(f"tasks_{task['group_id']}", group_tasks)
            if flush:
                file_db.save()
            return
    raise Exception(f"任务 {task['id']} 不存在")


def _log_task_operation(ctx: HandlerContext, task: dict, action: str):
    """记录任务管理操作；日志自身已包含操作时间。"""
    sender = getattr(ctx.event, 'sender', None)
    role = str(getattr(sender, 'role', '') or '').strip().lower() or 'unknown'
    logger.info(
        f"[cron-task] action={action} group_id={ctx.group_id} task_id={task['id']} "
        f"operator={ctx.user_id} role={role} creator={task['user_id']}"
    )


def _normalize_subscriber_ids(users) -> list[str]:
    """只接受具体数字 QQ，拒绝全体成员与异常 CQ 参数。"""
    normalized_users = []
    for user in users:
        normalized_user = str(user).strip()
        if normalized_user.lower() == 'all':
            raise ReplyException("不支持将全体成员订阅到定时提醒")
        if not normalized_user.isdigit():
            raise ReplyException(f"无效的订阅用户: {normalized_user or '(空)'}")
        if normalized_user not in normalized_users:
            normalized_users.append(normalized_user)
    return normalized_users


async def _get_subscriber_names(group_id: int, users: list[str]) -> dict[str, str]:
    """写库前确认目标成员可查询，避免接口失败后留下半完成订阅。"""
    return {
        user: await get_group_member_name(group_id, int(user))
        for user in users
    }

# 解析用户指示
async def parse_instruction(group_id, user_id, user_instruction):
    with open('config/cron/system_prompt.txt', 'r', encoding='utf-8') as f:
        system_prompt = f.read()
    system_prompt = system_prompt.format(time=datetime.now().strftime('%Y-%m-%d %H:%M:%S %A'))
    # print(system_prompt)

    session = ChatSession(system_prompt)
    session.append_user_content(user_instruction)

    model_name = get_model_preset('cron')

    max_retries = config.get('max_retries')
    for retry_count in range(max_retries):
        try:
            def process(resp: ChatSessionResponse):
                start = resp.result.find(r"{")
                end = resp.result.rfind(r"}")
                if start < 0 or end < start:
                    raise ValueError("模型未返回有效 JSON")
                task = loads_json(resp.result[start:end+1])
                if not isinstance(task, dict):
                    raise ValueError("模型返回结果不是 JSON 对象")
                if 'error' in task:
                    return {
                        'error': str(task.get('error') or 'invalid request'),
                        'reason': str(task.get('reason') or '无法根据当前描述创建提醒'),
                    }
                if not isinstance(task.get('content'), str) or not task['content'].strip():
                    return {
                        'error': 'invalid request',
                        'reason': '没有生成有效的提醒内容，请换一种说法后重试',
                    }
                task['group_id'] = group_id
                task['user_id'] = user_id
                task['sub_users'] = [ str(user_id) ]
                task['count'] = 0
                task['mute'] = False
                try:
                    _apply_schedule_metadata(task)
                except CronTaskValidationError as exc:
                    return {
                        'error': 'invalid request',
                        'reason': str(exc),
                    }
                return task
            return await session.get_response(model_name, process_func=process)
        except Exception as e:
            if retry_count < max_retries - 1:
                logger.warning(f"分析用户指示失败: {e}")
                continue
            else:
                raise

# ================================ 任务执行与补发 ================================ #

def _get_task_from_file_db(group_id: int, task_id: int) -> tuple[list[dict], dict | None]:
    """返回任务所在列表及列表内原对象，供调度回调原子更新。"""
    group_tasks = file_db.get(f"tasks_{group_id}", [])
    for task in group_tasks:
        if task['id'] == task_id:
            return group_tasks, task
    return group_tasks, None


def _schedule_one_time_retry(task: dict, group_tasks: list[dict], reason: str) -> bool:
    """仅对明确未发送的一次性任务安排有限次短期重试。"""
    if not _should_preserve_pending_one_time_task(task):
        logger.warning(
            f"一次性任务 {task['group_id']}_{task['id']} 已超过补发宽限期，不再重试: {reason}"
        )
        return False

    retry_count = int(task.get('send_retry_count', 0))
    max_retries = int(config.get('one_time_max_send_retries', 4))
    if retry_count >= max_retries:
        logger.warning(
            f"一次性任务 {task['group_id']}_{task['id']} 已达到最大补发次数 "
            f"{max_retries}: {reason}"
        )
        return False

    retry_delay_seconds = int(config.get('one_time_retry_delay_seconds', 60))
    retry_at = _scheduler_now() + timedelta(seconds=retry_delay_seconds)
    scheduled_at = _parse_task_datetime(task.get('scheduled_at'))
    grace_seconds = int(config.get('one_time_misfire_grace_seconds', 300))
    if scheduled_at is None or retry_at > scheduled_at + timedelta(seconds=grace_seconds):
        logger.warning(
            f"一次性任务 {task['group_id']}_{task['id']} 剩余宽限期不足，不再重试: {reason}"
        )
        return False

    scheduler.add_job(
        _run_cron_job,
        'date',
        run_date=retry_at,
        args=[task['group_id'], task['id']],
        id=_get_task_job_id(task['group_id'], task['id']),
        misfire_grace_time=grace_seconds,
        max_instances=1,
        replace_existing=True,
    )
    task['send_retry_count'] = retry_count + 1
    file_db.set(f"tasks_{task['group_id']}", group_tasks)
    file_db.save()
    logger.info(
        f"一次性任务 {task['group_id']}_{task['id']} 将于 {retry_at.isoformat()} "
        f"执行第 {task['send_retry_count']} 次补发: {reason}"
    )
    return True


async def _run_cron_job(group_id: int, task_id: int):
    """执行提醒；只有确认发送成功后才推进计数和一次性完成状态。"""
    try:
        group_tasks, task = _get_task_from_file_db(group_id, task_id)
        if task is None:
            logger.warning(f"群组 {group_id} 的任务 {task_id} 不存在")
            return
        if _is_completed_one_time_task(task):
            logger.info(f"跳过已完成的一次性任务 {group_id}_{task_id}")
            return

        # 群开关和任务静音只暂停发送，不删除持久化任务。
        if not gbl.check_id(group_id):
            logger.info(f"群组 {group_id} 的 Cron 已关闭，跳过任务 {task_id}")
            if _is_one_time_task(task):
                _schedule_one_time_retry(task, group_tasks, "本群 Cron 已关闭")
            return
        if task.get('mute'):
            logger.info(f"群组 {group_id} 的任务 {task_id} 已静音")
            if _is_one_time_task(task):
                _schedule_one_time_retry(task, group_tasks, "任务已静音")
            return

        current_time = _scheduler_now()
        last_notify_time = _parse_task_datetime(task.get('last_notify_time'))
        if (
            last_notify_time is not None
            and (current_time - last_notify_time).total_seconds() < 60
        ):
            logger.warning(f"跳过在60秒内已执行过的群组 {group_id} 的任务 {task_id}")
            return

        next_count = int(task.get('count', 0)) + 1
        logger.info(f"执行群组 {group_id} 的任务 {task_id} (第 {next_count} 次)")

        # 正文强制作为纯文本，仅程序生成的数字 QQ 才会构造 at 消息段。
        message = Message()
        rendered_content = _render_task_content(task.get('content', ''), current_time, next_count)
        message += MessageSegment.text(rendered_content + "\n")
        for subscriber in task.get('sub_users', []):
            normalized_subscriber = str(subscriber).strip()
            if not normalized_subscriber.isdigit():
                logger.warning(
                    f"跳过任务 {group_id}_{task_id} 中的非法订阅者: {normalized_subscriber!r}"
                )
                continue
            message += MessageSegment.at(int(normalized_subscriber))
        message += MessageSegment.text(
            f"\n【{task['id']}】{get_task_next_run_time_str(group_id, task_id)}"
        )

        send_result = await send_group_msg_by_bot(task['group_id'], message)
        if send_result is None:
            logger.warning(f"群组 {group_id} 的任务 {task_id} 未实际发送")
            if _is_one_time_task(task):
                _schedule_one_time_retry(task, group_tasks, "发送层未返回成功结果")
            return

        task['count'] = next_count
        task['last_notify_time'] = _scheduler_now().isoformat()
        if _is_one_time_task(task):
            task['completed_at'] = task['last_notify_time']
        file_db.set(f"tasks_{group_id}", group_tasks)
        if _is_one_time_task(task):
            # 一次性完成状态必须立即持久化，防止进程重启后重复补发。
            file_db.save()

    except Exception as exc:
        # 发送接口超时可能意味着消息实际已送达，因此不盲目重试，避免重复提醒。
        logger.print_exc(f"群组 {group_id} 的任务 {task_id} 执行失败: {exc}")


# ================================ 调度任务注册 ================================ #

async def add_cron_job(task, verbose=False):
    """注册新旧两种任务；旧数据没有元字段时继续沿用原 CronTrigger。"""
    if verbose:
        logger.info(f"添加cron任务: {task}")
    if _is_completed_one_time_task(task):
        logger.info(f"不再注册已完成的一次性任务 {task['group_id']}_{task['id']}")
        return

    common_options = {
        'args': [task['group_id'], task['id']],
        'id': _get_task_job_id(task['group_id'], task['id']),
        'max_instances': 1,
    }
    if _is_one_time_task(task):
        scheduled_at = _parse_task_datetime(task.get('scheduled_at'))
        if scheduled_at is None:
            raise CronTaskValidationError("一次性提醒缺少有效的计划时间")
        scheduler.add_job(
            _run_cron_job,
            'date',
            run_date=scheduled_at,
            misfire_grace_time=int(config.get('one_time_misfire_grace_seconds', 300)),
            replace_existing=True,
            **common_options,
        )
        return

    scheduler.add_job(
        _run_cron_job,
        'cron',
        misfire_grace_time=60,
        coalesce=True,
        replace_existing=True,
        **task['parameters'],
        **common_options,
    )

# 初始化已有的任务
@async_task("初始化cron任务", logger)
async def init_cron_jobs():
    for key in file_db.keys():
        if key.startswith("tasks_"):
            group_id = int(key.split("_")[-1])
            group_tasks = file_db.get(key, [])
            for task in group_tasks:
                try:
                    if _is_completed_one_time_task(task):
                        continue
                    if _is_one_time_task(task) and not _should_preserve_pending_one_time_task(task):
                        continue
                    await add_cron_job(task)
                except Exception as exc:
                    logger.print_exc(f"初始化群 {group_id} 的任务 {task['id']} 失败: {exc}")
            if len(group_tasks) > 0:
                logger.info(f"初始化群 {group_id} 的 {len(group_tasks)} 个任务完成")

# 删除cron任务
async def del_cron_job(group_id, task_id):
    job_id = _get_task_job_id(group_id, task_id)
    logger.info(f"删除cron任务: {job_id}")
    if not scheduler.get_job(job_id):
        logger.warning(f"任务 {job_id} 不存在")
        return
    scheduler.remove_job(job_id)

# 从文件数据库中删除cron任务
def del_cron_task_from_file_db(group_id, task_id):
    group_tasks = file_db.get(f"tasks_{group_id}", [])
    for i in range(len(group_tasks)):
        if group_tasks[i]['id'] == task_id:
            del group_tasks[i]
            file_db.set(f"tasks_{group_id}", group_tasks)
            file_db.save()
            return

# 定期检查过期任务
@repeat_with_interval(60, "定期检查过期任务", logger, delay=20)
async def check_expired_tasks():
    for key in file_db.keys():
        if key.startswith("tasks_"):
            group_id = int(key.split("_")[-1])
            group_tasks = file_db.get(key, [])
            for task in group_tasks:
                try:
                    job = scheduler.get_job(f"{group_id}_{task['id']}")
                    if job is None or job.next_run_time is None:
                        if _should_preserve_pending_one_time_task(task):
                            continue
                        await del_cron_job(group_id, task['id'])
                        del_cron_task_from_file_db(group_id, task['id'])
                        logger.info(f"删除过期任务: {group_id}_{task['id']}")

                        if gbl.check_id(group_id):
                            await send_group_msg_by_bot(
                                group_id,
                                f"cron任务【{task['id']}】过期，已删除",
                            )

                except Exception as e:
                    logger.print_exc(f"检查过期任务 {group_id}_{task['id']} 失败: {e}")


# 列出所有任务
def get_all_tasks() -> list[dict]:
    all_tasks = []
    for key in file_db.keys():
        if key.startswith("tasks_"):
            group_id = int(key.split("_")[-1])
            group_tasks = file_db.get(key, [])
            for task in group_tasks:
                all_tasks.append(deepcopy(task))
    return all_tasks


@on_collect_quited_group
def _on_collect_quited_group(groups: CurrentGroupInfoDict):
    quited_groups = []
    for task in get_all_tasks():
        group_id = int(task['group_id'])
        if group_id not in groups:
            quited_groups.append(group_id)
    return QuitedGroupUserInfo(quited_group_ids=quited_groups)

@on_clean_quited_group
async def _on_clean_quited_group(groups: CurrentGroupInfoDict):
    for task in get_all_tasks():
        group_id = int(task['group_id'])
        if group_id not in groups:
            await del_cron_job(group_id, task['id'])
            del_cron_task_from_file_db(group_id, task['id'])
            logger.info(f"删除已退出的群聊 {group_id} 中的任务 {task['id']}")


# 添加cron任务
cron_add = CmdHandler(["/cron", "/添加提醒", "/cron_add", "/cron add"], logger)
cron_add.check_cdrate(cd).check_wblist(gbl).check_group()
@cron_add.handle()
async def _(ctx: HandlerContext):
    text = ctx.get_args().strip()
    assert_and_reply(text, "请在/cron_add后输入指示")
    _assert_task_creation_limits(ctx.group_id, ctx.user_id)

    task = await parse_instruction(ctx.group_id, ctx.user_id, text)
    logger.info(f"获取cron参数: {task}")
    if 'error' in task:
        return await ctx.asend_reply_msg(
            f"添加失败: {task.get('reason') or '无法根据当前描述创建提醒'}"
        )

    # LLM 调用期间可能有并发创建成功，写库前必须重新核对额度。
    _assert_task_creation_limits(ctx.group_id, ctx.user_id)
    
    group_id_top = file_db.get(f"group_id_top_{ctx.group_id}", 0)
    group_tasks = file_db.get(f"tasks_{ctx.group_id}", [])
    existing_task_ids = [int(item.get('id', 0)) for item in group_tasks]
    task["id"] = max([int(group_id_top), *existing_task_ids], default=0) + 1

    file_db.set(f"group_id_top_{ctx.group_id}", task["id"])
    group_tasks.append(task)
    file_db.set(f"tasks_{ctx.group_id}", group_tasks)
    file_db.save()

    # 先落盘再注册，避免临近触发的一次性任务先执行却查不到自身。
    try:
        await add_cron_job(task, verbose=True)
    except Exception:
        del_cron_task_from_file_db(ctx.group_id, task['id'])
        raise
    _record_task_creation(ctx.user_id)

    resp = f"添加成功:\n" + task_to_str(task)
    return await ctx.asend_reply_msg(resp.strip())
   

# 删除任务（创建者、群管理或 superuser）
cron_del = CmdHandler(["/删除提醒", "/cron_del", "/cron del"], logger)
cron_del.check_cdrate(cd).check_wblist(gbl).check_group()
@cron_del.handle()
async def _(ctx: HandlerContext):
    task = await find_task(ctx, check_permission=True, allow_group_admin=True)
    await del_cron_job(ctx.group_id, task['id'])
    del_cron_task_from_file_db(ctx.group_id, task['id'])
    _log_task_operation(ctx, task, 'delete')
    return await ctx.asend_reply_msg(f"删除任务【{task['id']}】成功")


# 清空cron任务（仅超级用户）
cron_clear = CmdHandler(["/清空提醒", "/cron_clear", "/cron clear"], logger)
cron_clear.check_cdrate(cd).check_wblist(gbl).check_group().check_superuser()
@cron_clear.handle()
async def _(ctx: HandlerContext):
    group_tasks = file_db.get(f"tasks_{ctx.group_id}", [])
    for task in group_tasks:
        await del_cron_job(ctx.group_id, task['id'])
    file_db.set(f"tasks_{ctx.group_id}", [])
    file_db.save()
    return await ctx.asend_reply_msg("清空成功")


# 列出cron任务
cron_list = CmdHandler(["/提醒列表", "/cron_list", "/cron list"], logger)
cron_list.check_cdrate(cd).check_wblist(gbl).check_group()
@cron_list.handle()
async def _(ctx: HandlerContext):
    group_tasks = file_db.get(f"tasks_{ctx.group_id}", [])
    resp = f"本群共有 {len(group_tasks)} 个任务\n"
    for task in group_tasks:
        resp += task_to_str(task)
    return await ctx.asend_reply_msg(resp.strip())


# 订阅cron任务
cron_sub = CmdHandler(["/订阅提醒", "/cron_sub", "/cron sub"], logger)
cron_sub.check_cdrate(cd).check_wblist(gbl).check_group()
@cron_sub.handle()
async def _(ctx: HandlerContext):
    msg = ctx.get_msg()
    cqs = extract_cq_code(msg)
    users = [str(ctx.user_id)]
    for_other_user = False
    if 'at' in cqs:
        users = [str(cq.get('qq', '')) for cq in cqs['at']]
        for_other_user = True
    users = _normalize_subscriber_ids(users)
    task = await find_task(ctx, check_permission=for_other_user)
    user_names = await _get_subscriber_names(ctx.group_id, users)

    ok_users, already_users = [], []
    for user in users:
        if user in task['sub_users']:
            already_users.append(user)
        else:
            task['sub_users'].append(user)
            ok_users.append(user)
    update_task(task, flush=True)

    resp = ""
    if len(ok_users) > 0:
        resp += "添加订阅成功: "
        for user in ok_users:
            resp += _escape_cq_text(user_names[user]) + " "
        resp += "\n"
    if len(already_users) > 0:
        resp += "已订阅: "
        for user in already_users:
            resp += _escape_cq_text(user_names[user]) + " "
        resp += "\n"
    logger.info(f"为 {users} 订阅任务 {ctx.group_id}_{task['id']} 成功: 添加订阅成功 {ok_users} 已订阅 {already_users}")
    return await ctx.asend_reply_msg(resp.strip())
        

# 取消订阅cron任务
cron_unsub = CmdHandler(["/取消订阅提醒", "/cron_unsub", "/cron unsub"], logger)
cron_unsub.check_cdrate(cd).check_wblist(gbl).check_group()
@cron_unsub.handle()
async def _(ctx: HandlerContext):
    msg = ctx.get_msg()
    cqs = extract_cq_code(msg)
    users = [str(ctx.user_id)]
    for_other_user = False
    if 'at' in cqs:
        users = [str(cq.get('qq', '')) for cq in cqs['at']]
        for_other_user = True
    users = _normalize_subscriber_ids(users)
    task = await find_task(ctx, check_permission=for_other_user)
    user_names = await _get_subscriber_names(ctx.group_id, users)

    ok_users, already_users = [], []
    for user in users:
        if user in task['sub_users']:
            task['sub_users'].remove(user)
            ok_users.append(user)
        else:
            already_users.append(user)
    update_task(task, flush=True)

    resp = ""
    if len(ok_users) > 0:
        resp += "取消订阅成功: "
        for user in ok_users:
            resp += _escape_cq_text(user_names[user]) + " "
        resp += "\n"
    if len(already_users) > 0:
        resp += "未订阅: "
        for user in already_users:
            resp += _escape_cq_text(user_names[user]) + " "
        resp += "\n"
    logger.info(f"为 {users} 取消订阅任务 {ctx.group_id}_{task['id']} 成功: 取消订阅成功 {ok_users} 未订阅 {already_users}")
    return await ctx.asend_reply_msg(resp.strip())

    
# 清空任务订阅者（仅创建者或 superuser）
cron_unsuball = CmdHandler(
    ["/清空提醒订阅", "/cron_unsuball", "/cron unsuball", "/cron unsub all"],
    logger,
)
cron_unsuball.check_cdrate(cd).check_wblist(gbl).check_group()
@cron_unsuball.handle()
async def _(ctx: HandlerContext):
    task = await find_task(ctx, check_permission=True)
    task['sub_users'] = []
    update_task(task, flush=True)
    _log_task_operation(ctx, task, 'unsubscribe_all')
    return await ctx.asend_reply_msg("清空成功")


# 查看任务订阅者
cron_sublist = CmdHandler(["/提醒订阅列表", "/cron_sublist", "/cron sublist"], logger)
cron_sublist.check_cdrate(cd).check_wblist(gbl).check_group()
@cron_sublist.handle()
async def _(ctx: HandlerContext):
    task = await find_task(ctx, check_permission=False)
    resp = f"任务 {task['id']} 的订阅者:\n"
    for user in task['sub_users']:
        name = await get_group_member_name(ctx.group_id, int(user))
        resp += _escape_cq_text(f"{name}({user})\n")
    return await ctx.asend_reply_msg(resp.strip())

    
# 静音任务（创建者、群管理或 superuser）
cron_mute = CmdHandler(["/关闭提醒", "/cron_mute", "/cron mute"], logger)
cron_mute.check_cdrate(cd).check_wblist(gbl).check_group()
@cron_mute.handle()
async def _(ctx: HandlerContext):
    task = await find_task(ctx, check_permission=True, allow_group_admin=True)
    task['mute'] = True
    update_task(task, flush=True)
    _log_task_operation(ctx, task, 'mute')
    return await ctx.asend_reply_msg("静音成功")
    

# 静音全部任务（仅超级用户）
cron_muteall = CmdHandler(["/关闭所有提醒", "/cron_muteall", "/cron muteall"], logger)
cron_muteall.check_cdrate(cd).check_wblist(gbl).check_group().check_superuser()
@cron_muteall.handle()
async def _(ctx: HandlerContext):
    group_tasks = file_db.get(f"tasks_{ctx.group_id}", [])
    for task in group_tasks:
        task['mute'] = True
    file_db.set(f"tasks_{ctx.group_id}", group_tasks)
    file_db.save()
    return await ctx.asend_reply_msg("静音全部成功")

    
# 取消静音任务（创建者、群管理或 superuser）
cron_unmute = CmdHandler(["/开启提醒", "/cron_unmute", "/cron unmute"], logger)
cron_unmute.check_cdrate(cd).check_wblist(gbl).check_group()
@cron_unmute.handle()
async def _(ctx: HandlerContext):
    task = await find_task(ctx, check_permission=True, allow_group_admin=True)
    task['mute'] = False
    update_task(task, flush=True)
    if (
        _should_preserve_pending_one_time_task(task)
        and scheduler.get_job(_get_task_job_id(task['group_id'], task['id'])) is None
    ):
        await add_cron_job(task, verbose=True)
    _log_task_operation(ctx, task, 'unmute')
    return await ctx.asend_reply_msg("取消静音成功")
    

# 查看自己订阅的任务
cron_mysub = CmdHandler(["/我的提醒订阅", "/cron_mysub", "/cron mysub"], logger)
cron_mysub.check_cdrate(cd).check_wblist(gbl).check_group()
@cron_mysub.handle()
async def _(ctx: HandlerContext):
    group_tasks = file_db.get(f"tasks_{ctx.group_id}", [])
    resp = f"您订阅的任务:\n"
    for task in group_tasks:
        if str(ctx.user_id) in task['sub_users']:
            resp += task_to_str(task)
    return await ctx.asend_reply_msg(resp.strip())


# 修改任务文本（触发时间保持不变）
cron_edit = CmdHandler(["/修改提醒", "/cron_edit", "/cron edit"], logger)
cron_edit.check_cdrate(cd).check_wblist(gbl).check_group()
@cron_edit.handle()
async def _(ctx: HandlerContext):
    task = await find_task(ctx, check_permission=True)
    args = ctx.get_args().strip().split(maxsplit=1)
    if len(args) < 2 or not args[1].strip():
        raise ReplyException("请在任务ID后输入新的提醒内容")
    text = args[1].strip()
    task['content'] = text
    update_task(task, flush=True)
    _log_task_operation(ctx, task, 'edit_content')
    return await ctx.asend_reply_msg("提醒内容修改成功（触发时间未改变）")
