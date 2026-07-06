from utils import *
from config import *

from sekai_deck_recommend_cpp import (
    SekaiDeckRecommend, 
    DeckRecommendOptions, 
    DeckRecommendCardConfig, 
    DeckRecommendSingleCardConfig,
    DeckRecommendResult,
    DeckRecommendUserData,
)
from hashlib import md5
import multiprocessing as mp
from multiprocessing import Queue, Process
from concurrent.futures import ThreadPoolExecutor
from queue import Empty
from typing import Any
import threading
import setproctitle


class Worker:
    def log(self, *args, **kwargs):
        log(f"[worker-{self.worker_id}]", *args, **kwargs)

    def error(self, *args, **kwargs):
        error(f"<{self.worker_id}>", *args, **kwargs)

    def __init__(self, worker_id: int, worker_num: int):
        self.worker_id = worker_id
        self.worker_num = worker_num
        self.deckrec_seq_top = worker_id
        self.inited = False

    def init(self):
        if self.inited:
            return
        self.recommender = SekaiDeckRecommend()
        self.masterdata_version: dict[str, str] = {}
        self.masterdata_fingerprint: dict[str, str] = {}
        self.musicmetas_update_ts: dict[str, int] = {}
        self.userdata_cache: list[tuple[str, DeckRecommendUserData]] = []
        self.inited = True

    def _deckrec_options_to_str(self, userdata_hash: str, options: DeckRecommendOptions) -> str:
        def fmtbool(b: bool):
            return int(bool(b))
        def cardconfig2str(cfg: DeckRecommendCardConfig):
            return f"{fmtbool(cfg.disable)}{fmtbool(cfg.level_max)}{fmtbool(cfg.episode_read)}{fmtbool(cfg.master_max)}{fmtbool(cfg.skill_max)}"
        def singlecardcfg2str(cfg: List[DeckRecommendSingleCardConfig]):
            if not cfg:
                return "[]"
            return "[" + ", ".join(f"{c.card_id}:{cardconfig2str(c)}" for c in cfg) + "]"
        log = "("
        log += f"region={options.region}, "
        log += f"userdata_hash={userdata_hash}, "
        log += f"alg={options.algorithm}, "
        log += f"type={options.live_type}, "
        log += f"mid={options.music_id}, "
        log += f"mdiff={options.music_diff}, "
        log += f"eid={options.event_id}, "
        log += f"wl_cid={options.world_bloom_character_id}, "
        log += f"challenge_cid={options.challenge_live_character_id}, "
        log += f"limit={options.limit}, "
        # log += f"member={options.member}, "
        # log += f"rarity1={cardconfig2str(options.rarity_1_config)}, "
        # log += f"rarity2={cardconfig2str(options.rarity_2_config)}, "
        # log += f"rarity3={cardconfig2str(options.rarity_3_config)}, "
        # log += f"rarity4={cardconfig2str(options.rarity_4_config)}, "
        # log += f"rarity_bd={cardconfig2str(options.rarity_birthday_config)}, "
        # log += f"single_card_cfg={singlecardcfg2str(options.single_card_configs)}, "
        log += f"fixed_cards={options.fixed_cards})"
        return log

    def _update_data(self, region: str):
        db = load_json(DB_PATH, default={})

        masterdata_version = db.get('masterdata_version', {}).get(region)
        masterdata_fingerprint = db.get('masterdata_fingerprint', {}).get(region)

        # ================================ MasterData热更新兜底 ================================ #
        # 只比较版本号会漏掉“同版本下单文件热修”的场景。
        # 指纹变化时也要强制重新加载本地 masterdata，避免 worker 保留旧索引。
        if (
            self.masterdata_version.get(region) != masterdata_version or
            self.masterdata_fingerprint.get(region) != masterdata_fingerprint
        ):
            local_md_dir = pjoin(DATA_DIR, 'masterdata', region)
            self.recommender.update_masterdata(local_md_dir, region)
            self.masterdata_version[region] = masterdata_version
            self.masterdata_fingerprint[region] = masterdata_fingerprint
            self.log(
                f"加载 {region} MasterData: "
                f"v{masterdata_version} fp={(masterdata_fingerprint or 'None')[:8]}"
            )

        musicmetas_update_ts = db.get('musicmetas_update_ts', {}).get(region)
        if self.musicmetas_update_ts.get(region) != musicmetas_update_ts:
            local_mm_path = pjoin(DATA_DIR, f'musicmetas_{region}.json')
            self.recommender.update_musicmetas(local_mm_path, region)
            self.musicmetas_update_ts[region] = musicmetas_update_ts
            self.log(f"加载 {region} MusicMetas: {datetime.fromtimestamp(musicmetas_update_ts).strftime('%Y-%m-%d %H:%M:%S')}")

    def cache_userdata(self, userdata_bytes: bytes) -> dict:
        self.init()
        try:
            hash = md5(userdata_bytes).hexdigest()
            for h, _ in self.userdata_cache:
                if h == hash:
                    return {
                        'status': 'success',
                        'userdata_hash': hash,
                    }
            userdata = DeckRecommendUserData()
            userdata.load_from_bytes(userdata_bytes)
            self.userdata_cache.append((hash, userdata))
            # self.log(f"缓存用户数据: hash={hash}")
            while len(self.userdata_cache) > USERDATA_CACHE_NUM:
                h, _ = self.userdata_cache.pop(0)
                # self.log(f"移除用户数据缓存: hash={h}")
            return {
                'status': 'success',
                'userdata_hash': hash,
            }
        except BaseException as e:
            self.error("缓存用户数据失败:", get_exc_desc(e))
            return {
                'status': 'error',
                'message': get_exc_desc(e),
            }
    
    def recommend(self, region: str, options: dict, userdata_hash: str) -> dict:
        self.init()
        seq = self.deckrec_seq_top
        self.deckrec_seq_top += self.worker_num
        
        try:
            self._update_data(region)

            if not self.masterdata_version.get(region) or not self.musicmetas_update_ts.get(region):
                return {
                    'status': 'error',
                    'message': '组卡服务端数据未初始化完成，请稍后再试'
                }
            
            user_data = None
            for h, data in self.userdata_cache:
                if h == userdata_hash:
                    user_data = data
                    break
            if user_data is None:
                return {
                    'status': 'error',
                    'message': '组卡服务端找不到对应的用户数据缓存'
                }

            options = DeckRecommendOptions.from_dict(options)
            options.user_data = user_data
            self.log(f"组卡任务#{seq}: {self._deckrec_options_to_str(userdata_hash, options)}")

            start_time = datetime.now()
            res = self.recommender.recommend(options)
            cost_time = datetime.now() - start_time

            self.log(f"组卡任务#{seq}完成，耗时 {cost_time.total_seconds():.3f} 秒")

            return {
                'status': 'success',
                'result': res.to_dict(),
                'cost_time': cost_time.total_seconds(),
            }
        except BaseException as e:
            self.error(f"组卡任务#{seq}失败:", get_exc_desc(e))
            return {
                'status': 'error',
                'message': get_exc_desc(e),
            }


class WorkerContext:
    all_workers: dict[int, Worker] = {}
    all_processes: dict[int, Process] = {}
    available_workers: asyncio.Queue[Worker] = {}
    task_queues: dict[int, Queue] = {}
    result_queues: dict[int, Queue] = {}
    thread_pool: ThreadPoolExecutor = None
    worker_num: int = 0
    restart_lock = threading.Lock()

    @staticmethod
    def worker_loop(worker: Worker, task_queue: Queue, result_queue: Queue):
        setproctitle.setproctitle(f'lunabot-deckrec-worker-{worker.worker_id}')
        worker.log("Worker已启动")
        while True:
            task: tuple[str, tuple, dict] = task_queue.get()
            method_name, args, kwargs = task
            try:
                method = getattr(worker, method_name)
                result = method(*args, **kwargs)
                result_queue.put(result)
            except BaseException as e:
                worker.error(f"Worker 执行任务 {method_name} 失败:", get_exc_desc(e))
                result_queue.put({ 
                    'status': 'error',
                    'message': get_exc_desc(e),
                })

    @classmethod
    def init_workers(cls, worker_num: int):
        if mp.current_process().name == 'MainProcess':
            setproctitle.setproctitle('lunabot-deckrec-main')
        cls.worker_num = worker_num
        cls.all_workers = {}
        cls.all_processes = {}
        cls.task_queues = {}
        cls.result_queues = {}
        mp_ctx = mp.get_context('spawn')
        for i in range(worker_num):
            cls._start_worker(i, mp_ctx=mp_ctx)
            
        cls.available_workers = asyncio.Queue()
        for w in cls.all_workers.values():
            cls.available_workers.put_nowait(w)
        cls.thread_pool = ThreadPoolExecutor(max_workers=worker_num)

    # ================================ Worker生命周期管理 ================================ #
    # 组卡计算运行在独立子进程中。超时后不能只替换队列，否则旧子进程仍可能卡在旧队列上，
    # 并且上下文退出时还会把已失效的 worker 放回可用池，导致后续请求继续命中坏进程。
    @classmethod
    def _start_worker(cls, worker_id: int, *, mp_ctx=None) -> Worker:
        mp_ctx = mp_ctx or mp.get_context('spawn')
        worker_num = cls.worker_num or max(len(cls.all_workers), 1)
        worker = Worker(worker_id, worker_num)
        task_queue = mp_ctx.Queue()
        result_queue = mp_ctx.Queue()
        process = mp_ctx.Process(
            target=cls.worker_loop,
            args=(worker, task_queue, result_queue),
        )
        process.start()
        cls.all_workers[worker_id] = worker
        cls.task_queues[worker_id] = task_queue
        cls.result_queues[worker_id] = result_queue
        cls.all_processes[worker_id] = process
        return worker

    @staticmethod
    def _close_queue(queue: Any | None):
        if queue is None:
            return
        try:
            queue.close()
        except Exception:
            pass
        try:
            queue.cancel_join_thread()
        except Exception:
            pass

    @staticmethod
    def _stop_process(process: Any | None, *, worker_id: int):
        if process is None:
            return
        try:
            if process.is_alive():
                log(f"[worker-{worker_id}] Worker超时，正在终止旧子进程 pid={process.pid}")
                process.terminate()
                process.join(timeout=3.0)
                if process.is_alive():
                    log(f"[worker-{worker_id}] terminate 未结束，强制 kill pid={process.pid}")
                    process.kill()
                    process.join(timeout=1.0)
            else:
                process.join(timeout=0)
        except Exception as e:
            error(f"[worker-{worker_id}] 清理旧子进程失败:", get_exc_desc(e))
        finally:
            try:
                process.close()
            except Exception:
                pass

    @classmethod
    def _restart_worker(
        cls,
        worker_id: int,
        *,
        expected_process: Any | None,
        expected_task_queue: Any | None,
        expected_result_queue: Any | None,
    ) -> Worker:
        with cls.restart_lock:
            # 另一个并发请求可能已经完成了重启；此时不要误杀新进程。
            current_process = cls.all_processes.get(worker_id)
            if expected_process is not None and current_process is not expected_process:
                return cls.all_workers[worker_id]
            if expected_process is None and current_process is not None:
                return cls.all_workers[worker_id]

            cls._stop_process(expected_process, worker_id=worker_id)

            if cls.task_queues.get(worker_id) is expected_task_queue:
                cls._close_queue(expected_task_queue)
            if cls.result_queues.get(worker_id) is expected_result_queue:
                cls._close_queue(expected_result_queue)

            worker = cls._start_worker(worker_id)
            log(f"[worker-{worker_id}] Worker已在超时后重启 pid={cls.all_processes[worker_id].pid}")
            return worker
        
    def __init__(self, task_timeout: float = 60) -> None:
        self.worker: Worker | None = None
        self.task_timeout = task_timeout

    async def __aenter__(self):
        if not self.available_workers:
            raise RuntimeError("Please call WorkerContext.init_workers() first")
        worker = await self.available_workers.get()
        # 可用队列可能残留超时前的 Worker 对象；实际路由只依赖 worker_id，
        # 这里刷新为 all_workers 中的最新对象，便于日志和后续生命周期判断保持一致。
        self.worker = self.all_workers.get(worker.worker_id, worker)
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        if self.worker is not None:
            self.available_workers.put_nowait(self.all_workers.get(self.worker.worker_id, self.worker))

    @classmethod
    def workers(cls):
        for w in cls.all_workers.values():
            ctx = WorkerContext()
            ctx.worker = w
            yield ctx

    async def _get_result(self):
        worker_id = self.worker.worker_id
        result_queue = self.result_queues[worker_id]
        task_queue = self.task_queues[worker_id]
        process = self.all_processes.get(worker_id)
        try:
            # 不能用 asyncio.wait_for 包住无超时的 Queue.get：协程取消后，
            # 底层线程仍会永久阻塞，重复超时会把线程池耗尽。
            return await asyncio.get_event_loop().run_in_executor(
                self.thread_pool,
                result_queue.get,
                True,
                self.task_timeout,
            )
        except Empty:
            try:
                self.worker = self._restart_worker(
                    worker_id,
                    expected_process=process,
                    expected_task_queue=task_queue,
                    expected_result_queue=result_queue,
                )
            except Exception as e:
                # 重启失败时宁可临时损失一个 worker，也不能把已超时的坏 worker 放回池里。
                self.worker = None
                raise RuntimeError(f"Worker任务超时，且重启对应子进程失败: {get_exc_desc(e)}") from e
            raise RuntimeError("Worker任务超时，已重启对应子进程")

    async def cache_userdata(self, userdata_bytes: bytes) -> dict:
        self.task_queues[self.worker.worker_id].put(('cache_userdata', (userdata_bytes,), {},))
        return await self._get_result()
    
    async def recommend(self, region: str, options: dict, userdata_hash: str) -> dict:
        self.task_queues[self.worker.worker_id].put(('recommend', (region, options, userdata_hash,), {},))
        return await self._get_result()



