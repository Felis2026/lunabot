from .utils import *
from .master import MasterDataManager
from .gameapi import get_gameapi_config, request_gameapi, close_session
from .sql import (
    Ranking,
    close_conn,
    has_final_cutoff_sample,
    insert_rankings,
    query_latest_final_cutoff_sample,
    query_stable_final_cutoff_sample,
    reserve_final_cutoff_sample,
)
from .final_cutoff import (
    get_expected_final_cutoff_ranks,
    get_final_cutoff_sample_slot,
)
from tenacity import retry, wait_fixed, stop_after_attempt


set_log_level('INFO')

ALL_SERVER_REGIONS = ['cn', 'jp']

RECORD_INTERVAL_CFG = config.item('sk.record_interval_seconds')
HIGH_RES_RECORD_INTERVAL_CFG = config.item('sk.high_res_record.interval_seconds')
RECORD_TIME_AFTER_EVENT_END_CFG = config.item(
    'sk.record_time_after_event_end_minutes'
)
FINAL_CUTOFF_SAMPLE_INTERVAL_CFG = config.item(
    'sk.final_cutoff.sample_interval_minutes'
)
FINAL_CUTOFF_STABLE_SAMPLE_COUNT_CFG = config.item(
    'sk.final_cutoff.stable_sample_count'
)

mds = MasterDataManager('data/sekai/assets/masterdata')

latest_rankings_cache: dict[str, dict[int, dict[int, Ranking]]] = {}


# ================================ 处理逻辑 ================================ #

@dataclass(frozen=True)
class RankingUpdatePlan:
    """一次榜单更新计划；结榜确认轮次必须强制使用完整榜线响应。"""

    event_id: int
    is_high_res: bool
    final_cutoff_sample_slot: Optional[int] = None


def get_final_cutoff_sample_interval_seconds() -> int:
    """读取结榜确认间隔；旧配置缺少字段时固定回退到 30 分钟。"""

    minutes = int(FINAL_CUTOFF_SAMPLE_INTERVAL_CFG.get(30, False))
    return max(60, minutes * 60)


def get_final_cutoff_stable_sample_count() -> int:
    """读取连续一致次数；至少需要一次完整快照。"""

    return max(1, int(FINAL_CUTOFF_STABLE_SAMPLE_COUNT_CFG.get(3, False)))


def get_regular_record_end_time(event: dict) -> datetime:
    """常规榜线在结榜后继续记录一小段时间，默认延迟 10 分钟截止。"""

    end_time = datetime.fromtimestamp(event['aggregateAt'] / 1000 + 1)
    delay_minutes = max(
        0,
        int(RECORD_TIME_AFTER_EVENT_END_CFG.get(10, False)),
    )
    return end_time + timedelta(minutes=delay_minutes)


def get_wl_chapter_cid(region: str, wl_id: int) -> Optional[int]:
    """获取wl_id对应的角色cid，wl_id对应普通活动则返回None"""
    event_id = wl_id % 1000
    chapter_id = wl_id // 1000
    if chapter_id == 0:
        return None
    chapters = mds.get(region, 'worldBlooms').find_by('eventId', event_id, mode='all')
    assert chapters, f"活动{region}-{event_id}并不是WorldLink活动"
    chapter = find_by(chapters, "chapterNo", chapter_id)
    assert chapter, f"活动{region}-{event_id}并没有章节{chapter_id}"
    cid = chapter.get('gameCharacterId', None)
    return cid

def get_current_event(region: str, fallback: Optional[str] = None) -> dict:
    """
    获取当前活动 当前无进行中活动时 fallback = None:返回None prev:选择上一个 next:选择下一个 prev_first:优先选择上一个 next_first: 优先选择下一个
    """
    assert fallback is None or fallback in ("prev", "next", "prev_first", "next_first")
    events = sorted(mds.get(region, 'events').get(), key=lambda x: x['aggregateAt'], reverse=False)
    now = datetime.now()
    prev_event, cur_event, next_event = None, None, None
    for event in events:
        start_time = datetime.fromtimestamp(event['startAt'] / 1000)
        end_time = datetime.fromtimestamp(event['aggregateAt'] / 1000 + 1)
        if start_time <= now <= end_time:
            cur_event = event
        if end_time < now:
            prev_event = event
        if not next_event and start_time > now:
            next_event = event
    if fallback is None or cur_event:
        return cur_event
    if fallback == "prev" or (fallback == "prev_first" and prev_event):
        return prev_event
    if fallback == "next" or (fallback == "next_first" and next_event):
        return next_event
    return prev_event or next_event

def parse_rankings(region: str, event_id: int, data: dict) -> tuple[list[Ranking], list[Ranking]]:
    """从榜线数据解析Rankings，返回top100, border"""
    data_top100 = data.get('top100', {})
    data_border = data.get('border', {})
    assert data_top100, "获取榜线Top100数据失败"
    assert data_border, "获取榜线Border数据失败"

    now = datetime.now()

    # 普通活动
    if event_id < 1000:
        top100 = [Ranking.from_sk(item, now) for item in data_top100['rankings']]
        border = [Ranking.from_sk(item, now) for item in data_border['borderRankings'] if item['rank'] != 100]
    
    # WL活动
    else:
        cid = get_wl_chapter_cid(region, event_id)
        top100_rankings = find_by(data_top100.get('userWorldBloomChapterRankings', []), 'gameCharacterId', cid)
        top100 = [Ranking.from_sk(item, now) for item in top100_rankings['rankings']]
        border_rankings = find_by(data_border.get('userWorldBloomChapterRankingBorders', []), 'gameCharacterId', cid)
        border = [Ranking.from_sk(item, now) for item in border_rankings['borderRankings'] if item['rank'] != 100]

    for item in top100:
        item.uid = str(item.uid)
    for item in border:
        item.uid = str(item.uid)
    
    return top100, border

def build_split_ranking_urls(formatted_url: str) -> Optional[tuple[str, str]]:
    """
    根据ranking接口地址生成 top100/border 两个接口地址
    兼容 /ranking /rankings /ranking-top100 /ranking-border
    """
    if formatted_url.endswith("/ranking-top100"):
        prefix = formatted_url[:-len("/ranking-top100")]
        return (formatted_url, prefix + "/ranking-border")
    if formatted_url.endswith("/ranking-border"):
        prefix = formatted_url[:-len("/ranking-border")]
        return (prefix + "/ranking-top100", formatted_url)
    if formatted_url.endswith("/rankings"):
        prefix = formatted_url[:-len("/rankings")]
        return (prefix + "/ranking-top100", prefix + "/ranking-border")
    if formatted_url.endswith("/ranking"):
        prefix = formatted_url[:-len("/ranking")]
        return (prefix + "/ranking-top100", prefix + "/ranking-border")
    return None

def get_wl_events(region: str, event_id: int) -> list[dict]:
    """获取event_id对应的所有wl_event（时间顺序），如果不是wl则返回空列表"""
    event = mds.get(region, 'events').find_by_id(event_id)
    chapters = mds.get(region, 'worldBlooms').find_by('eventId', event['id'], mode='all')
    if not chapters:
        return []
    wl_events = []
    for chapter in chapters:
        wl_event = event.copy()
        wl_event['id'] = chapter['chapterNo'] * 1000 + event['id']
        wl_event['startAt'] = chapter['chapterStartAt']
        wl_event['aggregateAt'] = chapter['aggregateAt']
        # 单章节 finale 也需要保留类型与实际结束时间，供终线和归档流程识别。
        wl_event['chapterNo'] = chapter['chapterNo']
        wl_event['chapterEndAt'] = chapter.get('chapterEndAt')
        wl_event['worldBloomChapterType'] = chapter.get('worldBloomChapterType', 'game_character')
        wl_event['isSupplemental'] = chapter.get('isSupplemental', False)
        wl_event['wl_cid'] = chapter.get('gameCharacterId', None)
        wl_events.append(wl_event)
    return sorted(wl_events, key=lambda x: x['startAt'])

def check_ranking_in_high_res(region: str, ranking: Ranking) -> bool:
    """判断某条榜线记录是否需要高精度记录"""
    for rank_min, rank_max in config.get('sk.high_res_record.ranks', {}).get(region, []):
        if rank_min <= ranking.rank <= rank_max:
            return True
    for uid in config.get('sk.high_res_record.uids', {}).get(region, []):
        if str(ranking.uid) == str(uid):
            return True
    return False

def check_region_need_high_res(region: str) -> bool:
    """判断某个服务器是否需要高精度记录"""
    return config.get('sk.high_res_record.ranks', {}).get(region, []) or \
        config.get('sk.high_res_record.uids', {}).get(region, [])


# ================================ 榜线更新 ================================ #

class EventTracker:
    def __init__(self, region: str):
        self.region = region


    def info(self, *args, **kwargs):
        info(f"[{self.region.upper()}]", *args, **kwargs)

    def warning(self, *args, **kwargs):
        warning(f"[{self.region.upper()}]", *args, **kwargs)

    def error(self, *args, **kwargs):
        error(f"[{self.region.upper()}]", *args, **kwargs)
    
    def debug(self, *args, **kwargs):
        debug(f"[{self.region.upper()}]", *args, **kwargs)


    @retry(wait=wait_fixed(3), stop=stop_after_attempt(3), reraise=True)
    async def request_rankings(self, eid: int, url: str) -> tuple[dict, float]:
        """
        请求榜线数据，返回 (数据，耗时)
        """
        t = datetime.now().timestamp()
        formatted_url = url.format(event_id=eid)

        # 优先双接口：top100 + border（避免每次先撞单接口导致404）
        split_urls = build_split_ranking_urls(formatted_url)
        if split_urls:
            top100_url, border_url = split_urls
            try:
                top100_data = await request_gameapi(top100_url)
                border_data = await request_gameapi(border_url)
                data = {
                    'top100': top100_data,
                    'border': border_data,
                }
                return (data, datetime.now().timestamp() - t)
            except Exception:
                self.error(f"请求榜线双接口失败")
                return (None, datetime.now().timestamp() - t)

        # 兼容旧接口：直接返回 top100 + border
        try:
            data = await request_gameapi(formatted_url)
            if isinstance(data, dict) and data.get('top100') and data.get('border'):
                return (data, datetime.now().timestamp() - t)
            self.error(f"请求榜线数据失败: 非法返回结构 {formatted_url}")
            return (None, datetime.now().timestamp() - t)
        except Exception:
            self.error(f"请求榜线单接口失败")
            return (None, datetime.now().timestamp() - t)
        

    async def update_rankings(
        self,
        eid: int,
        data: dict,
        is_high_res: bool,
        final_cutoff_sample_slot: Optional[int] = None,
    ) -> tuple[int, int, float]:
        """
        更新总榜或WL单榜，返回 (活动id, 插入数量, 耗时)
        """
        t = datetime.now().timestamp()
        region = self.region
        try:
            top100, borders = parse_rankings(region, eid, data)

            # 30 分钟确认轮次只写独立样本表，不得继续追加普通榜线、
            # 更新常规观测时间或污染结榜后 10 分钟截止的历史曲线。
            if final_cutoff_sample_slot is not None:
                await insert_rankings(
                    region,
                    eid,
                    [],
                    observed_rankings=[*top100, *borders],
                    update_observation_times=False,
                    final_cutoff_sample_slot=final_cutoff_sample_slot,
                    final_cutoff_sample_interval_seconds=(
                        get_final_cutoff_sample_interval_seconds()
                    ),
                    final_cutoff_expected_ranks=get_expected_final_cutoff_ranks(
                        region,
                        board="chapter" if eid >= 1000 else "overall",
                    ),
                )
                return (
                    eid,
                    0,
                    datetime.now().timestamp() - t,
                )

            # 高精度记录模式：只记录必要的榜线
            if is_high_res:
                top100 = [item for item in top100 if check_ranking_in_high_res(region, item)]
                borders = [item for item in borders if check_ranking_in_high_res(region, item)]

            # 和缓存进行比对并更新缓存
            if region not in latest_rankings_cache:
                latest_rankings_cache[region] = {}
            if eid not in latest_rankings_cache[region]:
                latest_rankings_cache[region][eid] = {}

            # 前100强制更新
            rankings_to_insert: list[Ranking] = top100.copy()
            for item in top100:
                latest_rankings_cache[region][eid][item.rank] = item

            # borders仅发现任意榜线有更新才更新
            border_need_update = False
            for item in borders:
                last = latest_rankings_cache[region][eid].get(item.rank, None)
                if not last or last.score != item.score or last.uid != item.uid:
                    border_need_update = True
                    break
            if border_need_update:
                rankings_to_insert.extend(borders)
                for item in borders:
                    latest_rankings_cache[region][eid][item.rank] = item

            # 分数沿用原有 top100/border 追加策略；每轮成功响应另行更新轻量
            # 观测时间，供结榜固化区分“分数稳定”和“接口长期未返回该档位”。
            await insert_rankings(
                region,
                eid,
                rankings_to_insert,
                observed_rankings=[*top100, *borders],
            )

            return (eid, len(rankings_to_insert), datetime.now().timestamp() - t)

        except Exception as e:
            self.error(f"插入 {eid} 榜线数据失败: {get_exc_desc(e)}")
            return (eid, 0, datetime.now().timestamp() - t)


    async def build_update_plan(
        self,
        tracked_event: dict,
        requested_high_res: bool,
        now: datetime,
    ) -> Optional[RankingUpdatePlan]:
        """
        为总榜或章节榜生成本轮更新计划。

        活动中及结榜后 10 分钟沿用原采集精度；之后只在固定 30 分钟槽
        首次到达时校验完整榜线，三个连续槽稳定后立即停止后续请求。
        """

        start_time = datetime.fromtimestamp(tracked_event['startAt'] / 1000)
        event_id = int(tracked_event['id'])
        if now < start_time:
            return None
        if now <= get_regular_record_end_time(tracked_event):
            return RankingUpdatePlan(
                event_id=event_id,
                is_high_res=requested_high_res,
            )
        interval_seconds = get_final_cutoff_sample_interval_seconds()
        observed_at_ms = int(now.timestamp() * 1000)
        slot = get_final_cutoff_sample_slot(
            int(tracked_event['aggregateAt']),
            observed_at_ms,
            interval_seconds,
        )
        if slot is None:
            return None
        if await query_stable_final_cutoff_sample(
            self.region,
            event_id,
            interval_seconds,
            get_final_cutoff_stable_sample_count(),
        ):
            return None
        latest_sample = await query_latest_final_cutoff_sample(
            self.region,
            event_id,
            interval_seconds,
        )
        if (
            latest_sample
            and observed_at_ms
            < latest_sample.observed_at_ms + interval_seconds * 1000
        ):
            return None
        if await has_final_cutoff_sample(
            self.region,
            event_id,
            interval_seconds,
            slot,
        ):
            return None
        return RankingUpdatePlan(
            event_id=event_id,
            is_high_res=False,
            final_cutoff_sample_slot=slot,
        )


    async def update_region_ranking_task(self, is_high_res: bool) -> dict:
        """更新一次指定服务器的榜线数据，返回结果信息"""
        ret = { 'request_time': 0, 'inserts': [] }
        region = self.region
        url = get_gameapi_config(region).ranking_api_url
        if not url:
            return ret
            
        # 获取当前运行中的活动
        try:
            if not (event := get_current_event(region, fallback="prev")):
                self.info(f"当前无进行中或已结束活动，跳过榜线更新")
                await close_conn(region)
                return ret
        except Exception as e:
            self.warning(f"检查当前活动时失败: {get_exc_desc(e)}")

        # 清空并非当前活动的缓存榜线数据
        event_id = event['id']
        for key in list(latest_rankings_cache.get(region, {}).keys()):
            if key % 1000 != event_id:
                latest_rankings_cache[region].pop(key)
                self.info(f"清除非当前活动 {key} 的榜线缓存数据")

        now = datetime.now()
        plans = []
        if plan := await self.build_update_plan(event, is_high_res, now):
            plans.append(plan)
        for wl_event in get_wl_events(region, event_id):
            if plan := await self.build_update_plan(wl_event, is_high_res, now):
                plans.append(plan)

        # 结榜后的非检查槽不请求上游；下一次基础循环只做本地槽位判断。
        if not plans:
            return ret

        interval_seconds = get_final_cutoff_sample_interval_seconds()
        reserved_plans = []
        for plan in plans:
            if plan.final_cutoff_sample_slot is None:
                reserved_plans.append(plan)
                continue
            try:
                await reserve_final_cutoff_sample(
                    region,
                    plan.event_id,
                    interval_seconds,
                    plan.final_cutoff_sample_slot,
                    now,
                )
                reserved_plans.append(plan)
            except Exception as exc:
                self.error(
                    f"占用结榜确认槽失败 event={plan.event_id} "
                    f"slot={plan.final_cutoff_sample_slot}: {get_exc_desc(exc)}"
                )
        plans = reserved_plans
        if not plans:
            return ret

        data, request_time = await self.request_rankings(event_id, url)
        ret['request_time'] = request_time

        if not data:
            return ret

        tasks = [
            self.update_rankings(
                plan.event_id,
                data,
                plan.is_high_res,
                plan.final_cutoff_sample_slot,
            )
            for plan in plans
        ]
        for event_id, insert_num, cost_time in  await asyncio.gather(*tasks):
            ret['inserts'].append({
                'event_id': event_id,
                'insert_num': insert_num,
                'cost_time': cost_time
            })
        return ret


    async def start_track(self):
        self.info(f"榜线更新任务已启动")

        next_record_time = datetime.now()
        next_highres_record_time = datetime.now()
        next_time = datetime.now()

        while True:
            try:
                while datetime.now() < next_time:
                    await asyncio.sleep(0.5)

                need_high_res = check_region_need_high_res(self.region)

                start = datetime.now()
                is_high_res = need_high_res and datetime.now() < next_record_time

                result = await self.update_region_ranking_task(is_high_res)

                now = datetime.now()
                if not is_high_res:
                    next_record_time = start + timedelta(seconds=get_cfg_or_value(RECORD_INTERVAL_CFG))
                if need_high_res:
                    next_highres_record_time = start + timedelta(seconds=get_cfg_or_value(HIGH_RES_RECORD_INTERVAL_CFG))
                else:
                    next_highres_record_time = datetime.max
                next_time = min(next_record_time, next_highres_record_time)

                log_msg = f"完成{'高精度' if is_high_res else ''}更新"
                log_msg += f" | {(now - start).total_seconds():.2f}s | next: {next_time.strftime('%H:%M:%S')} | req: {result['request_time']:.2f}s"
                for insert_info in result.get('inserts', []):
                    log_msg += f" | event{insert_info['event_id']} +{insert_info['insert_num']} ({insert_info['cost_time']:.2f}s)"
                self.info(log_msg)

            except asyncio.CancelledError:
                break
        await close_session()



async def main():
    trackers = { region: EventTracker(region) for region in ALL_SERVER_REGIONS }
    tasks = []
    for region in ALL_SERVER_REGIONS:
        tasks.append(trackers[region].start_track())
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    print("\nStarting Event Tracker...")
    asyncio.run(main())
