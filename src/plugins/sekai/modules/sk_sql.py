from ...utils import *
from ..common import *
import aiosqlite
import json

from src.services.event_tracker.final_cutoff import (
    FinalCutoffSample,
    get_stable_final_cutoff_sample,
)

RANKING_NAME_LEN_LIMIT = 32

DB_PATH = SEKAI_DATA_DIR + "/db/sk_{region}/{event_id}_ranking.db"

_conns: Dict[str, aiosqlite.Connection] = {}
_created_table_keys: Dict[str, bool] = {}


@dataclass(frozen=True)
class ArchiveResult:
    """SQLite 归档前检查结果；success=False 时调用方绝不能继续压缩或删除。"""

    success: bool
    db_path: str
    journal_mode: str = ""
    wal_exists: bool = False
    shm_exists: bool = False
    integrity: str = ""
    row_count: int = 0
    max_id: Optional[int] = None
    max_ts: Optional[float] = None
    error: Optional[str] = None


async def get_conn(region, event_id, create) -> Optional[aiosqlite.Connection]:
    path = DB_PATH.format(region=region, event_id=event_id)
    create_parent_folder(path)
    if not create and not os.path.exists(path):
        return None

    global _conns
    if _conns.get(path) is None:
        _conns[path] = await aiosqlite.connect(path)
        await _conns[path].execute("PRAGMA journal_mode=WAL;") 
        logger.info(f"连接sqlite数据库 {path} 成功")

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
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS ranking_observation (
                rank INTEGER PRIMARY KEY,
                observed_ts REAL NOT NULL
            )
        """)
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


async def archive_database(region: str, event_id: int) -> ArchiveResult:
    """
    为压缩准备数据库：精确 checkpoint、切换 DELETE journal 并做完整性检查。

    任一步骤失败都会返回 success=False。调用方必须保留 DB/WAL/SHM，
    不能继续生成正式 ZIP；这是防止末尾榜线只留在 WAL 中的关键约束。
    """
    path = DB_PATH.format(region=region, event_id=event_id)
    if not os.path.exists(path):
        return ArchiveResult(
            success=False,
            db_path=path,
            error="数据库文件不存在",
        )
    
    global _conns, _created_table_keys
    if path in _conns:
        await _conns[path].close()
        del _conns[path]
        cache_key = f"{region}_{event_id}"
        if cache_key in _created_table_keys:
            del _created_table_keys[cache_key]

    journal_mode = ""
    integrity = ""
    row_count = 0
    max_id = None
    max_ts = None
    try:
        logger.info(f"尝试安全归档数据库 {path} ...")
        async with aiosqlite.connect(path, timeout=10) as conn:
            await conn.execute("PRAGMA busy_timeout = 10000;")

            cursor = await conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
            checkpoint = await cursor.fetchone()
            await cursor.close()
            # wal_checkpoint 返回 (busy, log_frames, checkpointed_frames)。
            if checkpoint and int(checkpoint[0]) != 0:
                raise RuntimeError(f"WAL checkpoint仍被占用: {checkpoint}")

            cursor = await conn.execute("PRAGMA journal_mode = DELETE;")
            row = await cursor.fetchone()
            await cursor.close()
            journal_mode = str(row[0]).lower() if row else ""
            if journal_mode != "delete":
                raise RuntimeError(f"无法切换到DELETE日志模式: {row}")

            cursor = await conn.execute("PRAGMA integrity_check;")
            row = await cursor.fetchone()
            await cursor.close()
            integrity = str(row[0]) if row else ""
            if integrity.lower() != "ok":
                raise RuntimeError(f"integrity_check失败: {integrity}")

            cursor = await conn.execute(
                "SELECT COUNT(*), MAX(id), MAX(ts) FROM ranking"
            )
            row_count, max_id, max_ts = await cursor.fetchone()
            await cursor.close()
            await conn.commit()
    except Exception as exc:
        wal_path = path + "-wal"
        shm_path = path + "-shm"
        return ArchiveResult(
            success=False,
            db_path=path,
            journal_mode=journal_mode,
            wal_exists=os.path.exists(wal_path) and os.path.getsize(wal_path) > 0,
            shm_exists=os.path.exists(shm_path),
            integrity=integrity,
            row_count=int(row_count or 0),
            max_id=max_id,
            max_ts=max_ts,
            error=get_exc_desc(exc),
        )

    wal_path = path + "-wal"
    shm_path = path + "-shm"
    wal_exists = os.path.exists(wal_path) and os.path.getsize(wal_path) > 0
    shm_exists = os.path.exists(shm_path)
    if wal_exists or shm_exists:
        return ArchiveResult(
            success=False,
            db_path=path,
            journal_mode=journal_mode,
            wal_exists=wal_exists,
            shm_exists=shm_exists,
            integrity=integrity,
            row_count=int(row_count or 0),
            max_id=max_id,
            max_ts=max_ts,
            error="checkpoint后仍残留非空WAL或SHM，可能有其他进程占用",
        )

    logger.info(f"数据库 {path} 安全归档检查完成。")
    return ArchiveResult(
        success=True,
        db_path=path,
        journal_mode=journal_mode,
        wal_exists=False,
        shm_exists=False,
        integrity=integrity,
        row_count=int(row_count or 0),
        max_id=max_id,
        max_ts=max_ts,
    )


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
    

def query_update_time(
    region: str, 
    event_id: int,
) -> Optional[datetime]:
    """检查表更新时间"""
    path = DB_PATH.format(region=region, event_id=event_id)
    if not os.path.exists(path):
        return None
    ret = datetime.fromtimestamp(os.path.getmtime(path))
    if os.path.exists(path + "-wal"):
        ret = max(ret, datetime.fromtimestamp(os.path.getmtime(path + "-wal")))
    return ret


async def query_ranking(
    region: str, 
    event_id: int, 
    uid: str = None,
    name: str = None,
    rank: int = None,
    start_time: datetime = None,
    end_time: datetime = None,
    limit: int = None,
    order_by: str = None,
) -> List[Ranking]:
    conn = await get_conn(region, event_id, create=False)
    if not conn:
        return []

    sql = "SELECT * FROM ranking WHERE 1=1"
    args = []

    if uid is not None:
        sql += " AND uid = ?"
        args.append(uid)

    if name is not None:
        name = name[:RANKING_NAME_LEN_LIMIT]
        sql += " AND name = ?"
        args.append(name)

    if rank is not None:
        sql += " AND rank = ?"
        args.append(rank)

    if start_time is not None:
        sql += " AND ts >= ?"
        args.append(start_time.timestamp())

    if end_time is not None:
        sql += " AND ts <= ?"
        args.append(end_time.timestamp())

    if order_by is not None:
        sql += f" ORDER BY {order_by}"

    if limit is not None:
        sql += f" LIMIT {limit}"

    cursor = await conn.execute(sql, args)
    rows = await cursor.fetchall()
    await cursor.close()

    return [Ranking.from_row(row) for row in rows]


async def query_latest_ranking(region: str, event_id: int, ranks: List[int] = None) -> List[Ranking]:
    conn = await get_conn(region, event_id, create=False)
    if not conn:
        return []
    if ranks:
        placeholders = ", ".join("?" for _ in ranks)
        sql = f"""
            SELECT * FROM (
                SELECT
                    *,
                    ROW_NUMBER() OVER (PARTITION BY rank ORDER BY ts DESC) as rn
                FROM ranking
                WHERE rank IN ({placeholders})
            )
            WHERE rn = 1
            ORDER BY rank
        """
        cursor = await conn.execute(sql, ranks)
        rows = await cursor.fetchall()
        await cursor.close()
        return [Ranking.from_row(row) for row in rows]
    else:
        # 对于表中的每一个rank，找到最新的一条记录
        cursor = await conn.execute("""
            SELECT * FROM ranking WHERE id IN (
                SELECT MAX(id) FROM ranking GROUP BY rank
            ) ORDER BY rank
        """)
        rows = await cursor.fetchall()
        await cursor.close()
        return [Ranking.from_row(row) for row in rows]


async def query_ranking_observation_times(
    region: str,
    event_id: int,
    ranks: List[int],
) -> Dict[int, datetime]:
    """读取各档最后一次被接口成功观测到的时间；旧库无记录时返回空字典。"""

    conn = await get_conn(region, event_id, create=False)
    if not conn or not ranks:
        return {}
    placeholders = ", ".join("?" for _ in ranks)
    cursor = await conn.execute(
        f"""
        SELECT rank, observed_ts
        FROM ranking_observation
        WHERE rank IN ({placeholders})
        """,
        ranks,
    )
    rows = await cursor.fetchall()
    await cursor.close()
    return {
        int(rank): datetime.fromtimestamp(float(observed_ts))
        for rank, observed_ts in rows
    }


# ================================ 结榜确认样本 ================================ #

def _final_cutoff_sample_from_row(row) -> FinalCutoffSample:
    """把榜线数据库中的确认采样行转换为共享结构。"""

    interval_seconds, slot, observed_ts, complete, scores_json = row
    return FinalCutoffSample(
        interval_seconds=int(interval_seconds),
        slot=int(slot),
        observed_at_ms=int(float(observed_ts) * 1000),
        complete=bool(complete),
        scores={
            int(rank): int(score)
            for rank, score in json.loads(scores_json).items()
        },
    )


async def query_stable_final_cutoff_sample(
    region: str,
    event_id: int,
    interval_seconds: int,
    required_count: int,
) -> Optional[FinalCutoffSample]:
    """返回最近连续 N 个检查槽中已经确认一致的最后一份完整快照。"""

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


async def query_first_ranking_after(
    region: str, 
    event_id: int, 
    after_time: datetime,
    ranks: List[int] = None,
) -> List[Ranking]:
    conn = await get_conn(region, event_id, create=False)
    if not conn:
        return []
    if ranks:
        # 对于ranks中的每一个rank，找到第一条记录
        placeholders = ", ".join("?" for _ in ranks)
        sql = f"""
            SELECT * FROM (
                SELECT
                    *,
                    ROW_NUMBER() OVER (PARTITION BY rank ORDER BY ts ASC) as rn
                FROM ranking
                WHERE rank IN ({placeholders}) AND ts > ?
            )
            WHERE rn = 1
            ORDER BY rank
        """
        params = ranks + [after_time.timestamp()]
        cursor = await conn.execute(sql, params)
        rows = await cursor.fetchall()
        await cursor.close()
        return [Ranking.from_row(row) for row in rows]
    else:
        # 对于表中的每一个rank，找到第一条记录
        cursor = await conn.execute("""
            SELECT * FROM ranking WHERE id IN (
                SELECT MIN(id) FROM ranking WHERE ts > ? GROUP BY rank
            ) ORDER BY rank
        """, (after_time.timestamp(),))
        rows = await cursor.fetchall()
        await cursor.close()
        return [Ranking.from_row(row) for row in rows]
    

async def query_ranks_with_interval(region: str, event_id: int, ranks: list[int], sample_interval_seconds: int):
    """
    以一定间隔采样ranks的记录
    """
    if not ranks:
        return {}
    conn = await get_conn(region, event_id, create=False)
    if not conn:
        return {}
    
    placeholders = ','.join('?' for _ in ranks)
    sql = f"""
        SELECT id, uid, name, score, rank, MIN(ts) as ts
        FROM ranking
        WHERE rank IN ({placeholders})
        GROUP BY rank, (ts / ?)
        ORDER BY ts ASC
    """
    
    args = list(ranks) + [sample_interval_seconds]
    
    async with conn.execute(sql, args) as cursor:
        rows = await cursor.fetchall()
        return [Ranking.from_row(row) for row in rows]
