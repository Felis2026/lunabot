from .utils import *
import aiosqlite
import json

from .final_cutoff import (
    FinalCutoffSample,
    get_stable_final_cutoff_sample,
)

RANKING_NAME_LEN_LIMIT = 32

SEKAI_DATA_DIR = "data/sekai"
DB_PATH = SEKAI_DATA_DIR + "/db/sk_{region}/{event_id}_ranking.db"

_conns: dict[str, aiosqlite.Connection] = {}
_created_table_keys: dict[str, bool] = {}


async def get_conn(region, event_id, create) -> Optional[aiosqlite.Connection]:
    path = DB_PATH.format(region=region, event_id=event_id)
    create_parent_folder(path)
    if not create and not os.path.exists(path):
        return None

    global _conns
    if _conns.get(path) is None:
        _conns[path] = await aiosqlite.connect(path)
        await _conns[path].execute("PRAGMA journal_mode=WAL;") 
        info(f"连接sqlite数据库 {path} 成功")

    conn = _conns[path]
    
    cache_key = f"{region}_{event_id}"
    
    if not _created_table_keys.get(cache_key):
        # 建表
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS ranking (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                uid TEXT,
                name TEXT,
                score INTEGER,
                rank INTEGER,
                ts INTEGER
            )
        """)
        # 创建索引
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_ranking_rank_ts 
            ON ranking (rank, ts)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_ranking_uid 
            ON ranking (uid)
        """)
        # 分数记录只在变化时追加；独立观测表记录接口最后一次成功返回该档位的
        # 时间，供结榜固化判断新鲜度，避免稳定分数被误判成陈旧数据。
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS ranking_observation (
                rank INTEGER PRIMARY KEY,
                observed_ts REAL NOT NULL
            )
        """)
        # 每个固定检查槽只保留首次完整响应，避免循环调度或进程重启后在同一
        # 30 分钟窗口重复计数。配置变更时由写入逻辑清理旧间隔的样本。
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS final_cutoff_sample (
                interval_seconds INTEGER NOT NULL,
                slot INTEGER NOT NULL,
                observed_ts REAL NOT NULL,
                complete INTEGER NOT NULL,
                scores_json TEXT NOT NULL,
                PRIMARY KEY (interval_seconds, slot)
            )
        """)
        await conn.commit()
        _created_table_keys[cache_key] = True

    return conn


async def close_conn(region: str):
    region_dir = os.path.normcase(os.path.dirname(
        DB_PATH.format(region=region, event_id=0)
    ))
    for key in list(_conns.keys()):
        if os.path.normcase(os.path.dirname(key)) == region_dir:
            await _conns[key].close()
            del _conns[key]
            info(f"关闭sqlite数据库连接 {key} 成功")
    for cache_key in list(_created_table_keys):
        if cache_key.startswith(f"{region}_"):
            del _created_table_keys[cache_key]


@dataclass
class Ranking:
    uid: str
    name: str
    score: int
    rank: int
    time: datetime
    id: Optional[int] = None

    @classmethod
    def from_row(cls, row):
        return cls(
            id=row[0],
            uid=row[1],
            name=row[2],
            score=row[3],
            rank=row[4],
            time=datetime.fromtimestamp(row[5])
        )
    
    @classmethod
    def from_sk(cls, data: dict, time: datetime = None):
        return cls(
            uid=data["userId"],
            name=data["name"],
            score=data["score"],
            rank=data["rank"],
            time=time or datetime.now(),
        )


async def insert_rankings(
    region: str,
    event_id: int,
    rankings: list[Ranking],
    *,
    observed_rankings: Optional[list[Ranking]] = None,
    update_observation_times: bool = True,
    final_cutoff_sample_slot: Optional[int] = None,
    final_cutoff_sample_interval_seconds: Optional[int] = None,
    final_cutoff_expected_ranks: Optional[list[int]] = None,
):
    """
    追加变化记录，并更新本轮接口实际观测到的各档时间。

    指定 final_cutoff_sample_slot 时会在同一事务中保存一份结榜确认样本；
    校验专用调用可关闭 update_observation_times，避免 30 分钟样本混入常规
    历史的截止时间。同一检查槽只接受首次响应。
    """

    conn = await get_conn(region, event_id, create=True)

    for ranking in rankings:
        ranking.name = ranking.name[:RANKING_NAME_LEN_LIMIT]
        await conn.execute("""
            INSERT INTO ranking (uid, name, score, rank, ts) VALUES (?, ?, ?, ?, ?)
        """, (ranking.uid, ranking.name, ranking.score, ranking.rank, ranking.time.timestamp()))

    observations = observed_rankings if observed_rankings is not None else rankings
    if update_observation_times:
        latest_observed_by_rank: dict[int, float] = {}
        for ranking in observations:
            observed_ts = ranking.time.timestamp()
            latest_observed_by_rank[ranking.rank] = max(
                observed_ts,
                latest_observed_by_rank.get(ranking.rank, observed_ts),
            )
        if latest_observed_by_rank:
            await conn.executemany(
                """
                INSERT INTO ranking_observation (rank, observed_ts)
                VALUES (?, ?)
                ON CONFLICT(rank) DO UPDATE SET observed_ts = excluded.observed_ts
                """,
                sorted(latest_observed_by_rank.items()),
            )

    if final_cutoff_sample_slot is not None:
        if not final_cutoff_sample_interval_seconds:
            raise ValueError("结榜确认样本缺少采样间隔")
        expected_ranks = {
            int(rank)
            for rank in (final_cutoff_expected_ranks or [])
        }
        if not expected_ranks:
            raise ValueError("结榜确认样本缺少预期档位")
        observed_by_rank = {
            int(ranking.rank): ranking
            for ranking in observations
            if int(ranking.rank) in expected_ranks
        }
        scores = {
            rank: int(observed_by_rank[rank].score)
            for rank in sorted(observed_by_rank)
        }
        observed_ts = max(
            (ranking.time.timestamp() for ranking in observed_by_rank.values()),
            default=datetime.now().timestamp(),
        )
        interval_seconds = int(final_cutoff_sample_interval_seconds)
        # 间隔配置改变后必须重新累计，不能把不同采样计划的槽位拼成三次一致。
        await conn.execute(
            "DELETE FROM final_cutoff_sample WHERE interval_seconds != ?",
            (interval_seconds,),
        )
        await conn.execute(
            """
            INSERT INTO final_cutoff_sample (
                interval_seconds,
                slot,
                observed_ts,
                complete,
                scores_json
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(interval_seconds, slot) DO UPDATE SET
                observed_ts = excluded.observed_ts,
                complete = excluded.complete,
                scores_json = excluded.scores_json
            WHERE final_cutoff_sample.complete = 0
              AND final_cutoff_sample.scores_json = '{}'
            """,
            (
                interval_seconds,
                int(final_cutoff_sample_slot),
                observed_ts,
                int(expected_ranks.issubset(scores)),
                json.dumps(scores, ensure_ascii=False, separators=(",", ":")),
            ),
        )

    await conn.commit()


# ================================ 结榜确认样本 ================================ #

def _final_cutoff_sample_from_row(row) -> FinalCutoffSample:
    """把 SQLite 行解析为共享确认样本。"""

    interval_seconds, slot, observed_ts, complete, scores_json = row
    scores = {
        int(rank): int(score)
        for rank, score in json.loads(scores_json).items()
    }
    return FinalCutoffSample(
        interval_seconds=int(interval_seconds),
        slot=int(slot),
        observed_at_ms=int(float(observed_ts) * 1000),
        complete=bool(complete),
        scores=scores,
    )


async def reserve_final_cutoff_sample(
    region: str,
    event_id: int,
    interval_seconds: int,
    slot: int,
    attempted_at: datetime,
) -> None:
    """
    在请求上游前占用固定检查槽。

    即使请求失败或进程中途退出，该槽也会作为不完整样本保留，避免同一个
    30 分钟窗口被基础/高精度循环反复重试而增加上游访问。
    """

    conn = await get_conn(region, event_id, create=True)
    interval_seconds = int(interval_seconds)
    await conn.execute(
        "DELETE FROM final_cutoff_sample WHERE interval_seconds != ?",
        (interval_seconds,),
    )
    await conn.execute(
        """
        INSERT OR IGNORE INTO final_cutoff_sample (
            interval_seconds,
            slot,
            observed_ts,
            complete,
            scores_json
        ) VALUES (?, ?, ?, 0, '{}')
        """,
        (
            interval_seconds,
            int(slot),
            attempted_at.timestamp(),
        ),
    )
    await conn.commit()


async def has_final_cutoff_sample(
    region: str,
    event_id: int,
    interval_seconds: int,
    slot: int,
) -> bool:
    """判断指定固定检查槽是否已经落盘。"""

    conn = await get_conn(region, event_id, create=False)
    if not conn:
        return False
    cursor = await conn.execute(
        """
        SELECT 1
        FROM final_cutoff_sample
        WHERE interval_seconds = ? AND slot = ?
        LIMIT 1
        """,
        (int(interval_seconds), int(slot)),
    )
    row = await cursor.fetchone()
    await cursor.close()
    return row is not None


async def query_latest_final_cutoff_sample(
    region: str,
    event_id: int,
    interval_seconds: int,
) -> Optional[FinalCutoffSample]:
    """读取当前采样计划最后一次已经尝试的检查槽。"""

    conn = await get_conn(region, event_id, create=False)
    if not conn:
        return None
    cursor = await conn.execute(
        """
        SELECT interval_seconds, slot, observed_ts, complete, scores_json
        FROM final_cutoff_sample
        WHERE interval_seconds = ?
        ORDER BY slot DESC
        LIMIT 1
        """,
        (int(interval_seconds),),
    )
    row = await cursor.fetchone()
    await cursor.close()
    return _final_cutoff_sample_from_row(row) if row else None


async def query_stable_final_cutoff_sample(
    region: str,
    event_id: int,
    interval_seconds: int,
    required_count: int,
) -> Optional[FinalCutoffSample]:
    """读取最近检查槽，并返回已满足连续一致条件的最后一份样本。"""

    conn = await get_conn(region, event_id, create=False)
    if not conn:
        return None
    cursor = await conn.execute(
        """
        SELECT interval_seconds, slot, observed_ts, complete, scores_json
        FROM final_cutoff_sample
        WHERE interval_seconds = ?
        ORDER BY slot DESC
        LIMIT ?
        """,
        (int(interval_seconds), int(required_count)),
    )
    rows = await cursor.fetchall()
    await cursor.close()
    return get_stable_final_cutoff_sample(
        (_final_cutoff_sample_from_row(row) for row in rows),
        int(required_count),
    )

