from ..utils import *
from .common import *
import threading
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from contextvars import ContextVar

asset_config = Config('sekai.asset')
ASSET_DEBUG_CFG = asset_config.item('debug')

# ================================ MasterData资源 ================================ #

DEFAULT_VERSION = "0.0.0.0"
MASTER_DB_CACHE_DIR = f"{SEKAI_ASSET_DIR}/masterdata/"
# compact 区服的 costume3ds 可能缺少 assetbundleName，
# 这里使用独立的 sidecar 补丁目录持久化补全结果，不改写上游原始 masterdata 文件。
MASTER_DB_PATCH_DIR = f"{SEKAI_ASSET_DIR}/masterdata_patch/"
COSTUME3DS_ASSET_PATCH_VERSION = 2
DEFAULT_INDEX_KEYS = ['id']
DEFAULT_SORT_KEYS = []
# 连续失败后由后续访问触发重试，等待间隔依次为 30、120、300 秒，此后按 300 秒封顶。
MASTERDATA_RETRY_SECONDS = (30, 120, 300)
_masterdata_cache_metadata_lock = asyncio.Lock()
ASSET_REQUEST_LIMIT = ContextVar('sekai_asset_request_limit', default=None)
ASSET_PREPARE_TIMINGS = ContextVar('sekai_asset_prepare_timings', default=None)
ASSET_RESOURCE_LIMIT = asyncio.Semaphore(4)
ASSET_PREPARE_IN_POOL = ContextVar('sekai_asset_prepare_in_pool', default=False)


@asynccontextmanager
async def resource_prepare_scope():
    """为纯资源准备共享下载额度，读取和解码使用现有线程池。"""
    limit_token = ASSET_REQUEST_LIMIT.set(ASSET_RESOURCE_LIMIT)
    pool_token = ASSET_PREPARE_IN_POOL.set(True)
    try:
        yield
    finally:
        ASSET_PREPARE_IN_POOL.reset(pool_token)
        ASSET_REQUEST_LIMIT.reset(limit_token)


async def prepare_assets(requests):
    """执行按唯一键登记的资源工厂，返回同顺序结果；工厂中不能创建绘图组件。"""
    keys = list(requests)
    async with resource_prepare_scope():
        results = await bounded_map(lambda key: requests[key](), keys, concurrency=4)
    return dict(zip(keys, results))


def record_asset_timing(stage, name, seconds):
    """把实际工作任务的耗时加入所属卡池请求，其他等待者仅记录自己的等待总时长。"""
    timings = ASSET_PREPARE_TIMINGS.get()
    if timings is not None:
        timings.append((stage, name, round(seconds, 4)))


@asynccontextmanager
async def asset_request_slot():
    """按请求上下文限制实际下载，等待共享任务的调用者不重复占用额度。"""
    limit = ASSET_REQUEST_LIMIT.get()
    if limit is None:
        yield
    else:
        started = time.perf_counter()
        async with limit:
            record_asset_timing('queue', 'request', time.perf_counter()-started)
            yield

# 运行期缓存：按区服缓存 costume3ds 资源名补丁，避免重复读取和重复生成。
_costume3ds_asset_patch_cache: Dict[str, Dict[int, str]] = {}
_costume3ds_asset_patch_meta: Dict[str, tuple] = {}
_costume3ds_asset_patch_lock = threading.Lock()

def get_multi_keys(data: dict, keys: List[Any]):
    for key in keys:
        if key in data:
            return data[key]
    raise KeyError(f"None of the keys {keys} found in dict")

def get_version_order(version: str) -> tuple:
    return tuple(map(int, version.split(".")))

def get_masterdata_cache_version(region: str, name: str) -> str:
    return file_db.get_copy("master_data_cache_versions", {}).get(region, {}).get(name, DEFAULT_VERSION)

def get_masterdata_cache_path(region: str, name: str) -> str:
    return pjoin(MASTER_DB_CACHE_DIR, region, f"{name}.json")

def get_masterdata_patch_path(region: str, name: str) -> str:
    return pjoin(MASTER_DB_PATCH_DIR, region, f"{name}.json")

def build_costume3ds_assetbundle_patch(
    region: str,
    region_costume3ds=None,
    jp_costume3ds=None,
) -> Dict[int, str]:
    """生成 compact 区服的衣装资源名补丁，返回衣装 ID 到资源名的映射。

    优先使用传入的候选数据，省略时读取对应区服的缓存文件。
    缺失名称按 JP 同 ID 补全，数字名称按部位转换并以 JP 资源名验证。
    所需数据文件缺失时返回空字典。
    """
    region_path = get_masterdata_cache_path(region, "costume3ds")
    jp_path = get_masterdata_cache_path("jp", "costume3ds")
    if region_costume3ds is None and not os.path.exists(region_path):
        logger.warning(f"MasterData [{region}.costume3ds] 补丁生成失败: 区服数据不存在")
        return {}
    if jp_costume3ds is None and not os.path.exists(jp_path):
        logger.warning(f"MasterData [{region}.costume3ds] 补丁生成失败: JP 数据不存在")
        return {}

    if region_costume3ds is None:
        region_costume3ds = load_json(region_path)
    if jp_costume3ds is None:
        jp_costume3ds = load_json(jp_path)

    # 按 ID 和部位建立 JP 资源名参照。
    jp_asset_map: Dict[int, str] = {}
    jp_asset_names_by_part: set[tuple[str, str]] = set()
    for item in jp_costume3ds:
        asset_name = item.get("assetbundleName") or item.get("_assetbundleName")
        if asset_name:
            jp_asset_map[item["id"]] = asset_name
            jp_asset_names_by_part.add((asset_name, item.get("partType")))

    # 分别补全缺失名称和经过验证的纯数字名称。
    patch: Dict[int, str] = {}
    missing_count = 0
    missing_patched_count = 0
    numeric_count = 0
    numeric_patched_count = 0
    for item in region_costume3ds:
        asset_name = item.get("assetbundleName") or item.get("_assetbundleName")
        if not asset_name:
            missing_count += 1
            jp_asset_name = jp_asset_map.get(item["id"])
            if jp_asset_name:
                patch[item["id"]] = jp_asset_name
                missing_patched_count += 1
        elif isinstance(asset_name, str) and asset_name.isdigit():
            numeric_count += 1
            part_type = item.get("partType")
            candidate = f"cos{asset_name}_{part_type}"
            # CN 与 JP 的衣装 id、seq 不一定相同；不能靠减固定偏移或 seq 猜对应关系。
            # 保留编号前导零，仅替换 JP 中确实存在且部位一致的完整资源名。
            if (candidate, part_type) in jp_asset_names_by_part:
                patch[item["id"]] = candidate
                numeric_patched_count += 1

    logger.info(
        f"MasterData [{region}.costume3ds] 补丁生成完成: 缺失{missing_count}条, "
        f"JP补齐{missing_patched_count}条, 仍缺失{missing_count - missing_patched_count}条, "
        f"数字名称{numeric_count}条, 已验证转换{numeric_patched_count}条"
    )
    return patch

def load_costume3ds_assetbundle_patch(
    region: str,
    region_costume3ds=None,
    region_version: str | None = None,
    source_identity: str | None = None,
) -> Dict[int, str]:
    """加载或生成衣装资源名补丁，返回按区服共享的缓存映射。

    已确认的文件候选按文件身份复用补丁，新下载的候选按自身内容重建。
    JP 参照优先使用内存快照，已落盘快照使用文件身份支持跨重启复用。
    新补丁会尝试保存到 sidecar，保存失败时仍返回生成结果。
    """
    candidate_supplied = region_costume3ds is not None
    region_version = region_version or get_masterdata_cache_version(region, "costume3ds")
    jp_snapshot = MasterDataManager.get("costume3ds")._snapshots.get("jp")
    jp_costume3ds = jp_snapshot[0] if jp_snapshot else None
    jp_version = jp_snapshot[3] if jp_snapshot else get_masterdata_cache_version("jp", "costume3ds")
    # 候选转换先于原始表落盘，必须直接使用候选内容，不能回读上一版文件。
    # 内存参照用对象身份区分，磁盘参照用文件状态识别同版本号的内容替换。
    def reference_identity(source_region, data):
        """已确认的快照使用文件身份，尚未保存的快照使用内存身份。"""
        try:
            stat = os.stat(get_masterdata_cache_path(source_region, "costume3ds"))
            if data is not None:
                manager = MasterDataManager.get("costume3ds")
                if (manager.file_version.get(source_region) != manager.version.get(source_region)
                        or manager._file_signatures.get(source_region) != (stat.st_size, stat.st_mtime_ns)):
                    return f"memory:{id(data)}"
            return f"file:{stat.st_size}:{stat.st_mtime_ns}"
        except OSError:
            return f"memory:{id(data)}" if data is not None else "missing"

    region_identity = source_identity or (
        f"candidate:{id(region_costume3ds)}" if candidate_supplied else reference_identity(region, None))
    identities = (region_identity, reference_identity("jp", jp_costume3ds))
    current_meta = (region_version, jp_version, COSTUME3DS_ASSET_PATCH_VERSION, *identities)
    reusable = not candidate_supplied or bool(source_identity and source_identity.startswith('file:'))

    # 复用补丁前核对来源身份与规则版本，避免同版本数据替换后继续使用旧结果。
    with _costume3ds_asset_patch_lock:
        if reusable and _costume3ds_asset_patch_meta.get(region) == current_meta:
            return _costume3ds_asset_patch_cache.get(region, {})

        patch_path = get_masterdata_patch_path(region, "costume3ds_assetbundle_patch")
        patch: Dict[int, str] | None = None

        if reusable and os.path.exists(patch_path):
            try:
                patch_data = load_json(patch_path)
                if (
                    patch_data.get("region_version") == region_version
                    and patch_data.get("jp_version") == jp_version
                    and patch_data.get("patch_version") == COSTUME3DS_ASSET_PATCH_VERSION
                    and patch_data.get("source_identities") == list(identities)
                ):
                    patch = {
                        int(costume3d_id): asset_name
                        for costume3d_id, asset_name in patch_data.get("assetbundle_map", {}).items()
                        if asset_name
                    }
            except Exception as e:
                logger.warning(f"MasterData [{region}.costume3ds] 读取补丁缓存失败: {get_exc_desc(e)}")

        if patch is None:
            patch = build_costume3ds_assetbundle_patch(region, region_costume3ds, jp_costume3ds)
            try:
                if not reusable:
                    _costume3ds_asset_patch_cache[region] = patch
                    _costume3ds_asset_patch_meta[region] = current_meta
                    return patch
                dump_json({
                    "region_version": region_version,
                    "jp_version": jp_version,
                    "patch_version": COSTUME3DS_ASSET_PATCH_VERSION,
                    "source_identities": list(identities),
                    "assetbundle_map": {str(costume3d_id): asset_name for costume3d_id, asset_name in patch.items()},
                }, patch_path, indent=False)
            except Exception as exc:
                # sidecar 是可重建缓存，写失败不能撤掉已经算好的资源名补丁。
                logger.warning(f"MasterData [{region}.costume3ds] 保存补丁缓存失败: {get_exc_desc(exc)}")

        _costume3ds_asset_patch_cache[region] = patch
        _costume3ds_asset_patch_meta[region] = current_meta
        return patch

def persist_costume3ds_assetbundle_patch(region, version, candidate_identity, signature):
    """原始文件保存后，将对应候选补丁登记为可跨重启复用的文件补丁。"""
    with _costume3ds_asset_patch_lock:
        meta = _costume3ds_asset_patch_meta.get(region)
        if not meta or meta[0] != version or meta[3] != candidate_identity:
            return
        identity = f'file:{signature[0]}:{signature[1]}'
        meta = (*meta[:3], identity, meta[4])
        try:
            dump_json({
                'region_version': meta[0], 'jp_version': meta[1], 'patch_version': meta[2],
                'source_identities': list(meta[3:]),
                'assetbundle_map': {str(identifier): name for identifier, name in
                                    _costume3ds_asset_patch_cache.get(region, {}).items()},
            }, get_masterdata_patch_path(region, 'costume3ds_assetbundle_patch'), indent=False)
            _costume3ds_asset_patch_meta[region] = meta
        except Exception as exc:
            logger.warning(f'MasterData [{region}.costume3ds] 保存补丁缓存失败: {get_exc_desc(exc)}')


def make_costume3ds_map_fn(region: str):
    """创建指定区服的衣装转换函数，转换时会原地修改记录并可能生成 sidecar。"""
    def costume3ds_map_fn(costume3ds: List[Dict[str, Any]], *, version: str | None = None,
                         source_identity: str | None = None):
        """原地统一资源名字段并应用补丁，返回传入的衣装列表。"""
        # 先统一资源名字段，再按缺失和纯数字名称的数量判断是否需要补丁。
        normalized_count = 0
        missing_count = 0
        numeric_count = 0
        for item in costume3ds:
            asset_name = item.get("assetbundleName") or item.get("_assetbundleName")
            if asset_name and item.get("assetbundleName") != asset_name:
                item["assetbundleName"] = asset_name
                normalized_count += 1
            if not item.get("assetbundleName"):
                missing_count += 1
            elif isinstance(asset_name, str) and asset_name.isdigit():
                numeric_count += 1

        if missing_count == 0 and numeric_count == 0:
            return costume3ds

        patch = load_costume3ds_assetbundle_patch(region, costume3ds, version, source_identity)
        patched_count = 0
        numeric_patched_count = 0
        for item in costume3ds:
            current_name = item.get("assetbundleName")
            if current_name and not (isinstance(current_name, str) and current_name.isdigit()):
                continue
            asset_name = patch.get(item["id"])
            if asset_name:
                item["assetbundleName"] = asset_name
                if current_name:
                    numeric_patched_count += 1
                else:
                    patched_count += 1

        remaining_count = sum(1 for item in costume3ds if not item.get("assetbundleName"))
        logger.info(
            f"MasterData [{region}.costume3ds] 映射完成: 本地归一化{normalized_count}条, "
            f"补丁补齐{patched_count}条, 数字名称转换{numeric_patched_count}条, "
            f"剩余缺失{remaining_count}条"
        )
        return costume3ds

    return costume3ds_map_fn

@dataclass
class RegionMasterDbSource:
    """
    区服MasterDB数据源类型
    """
    name: str
    base_url: str
    version_url: str
    version: str = DEFAULT_VERSION
    asset_version: str = DEFAULT_VERSION

    async def update_version(self):
        version = DEFAULT_VERSION
        try:
            timeout = asset_config.get('default_masterdata_update_check_timeout')
            version_data = await asyncio.wait_for(download_json(self.version_url), timeout)
            version = version_data.get('cdnVersion', 0)
            if not version:
                version = get_multi_keys(version_data, ['data_version', 'dataVersion'])
            self.version = str(version)
            self.asset_version = get_multi_keys(version_data, ['asset_version', 'assetVersion'])
            # logger.info(f"MasterDB [{self.name}] 的版本为 {version}")
        except asyncio.TimeoutError:
            logger.error(f"获取 MasterDB [{self.name}] 的版本信息超时")
        except Exception as e:
            logger.print_exc(f"获取 MasterDB [{self.name}] 的版本信息失败")

class RegionMasterDbManager:
    """
    MasterDB数据源管理器
    - 使用 ```get(region)``` 方法获取对应区服的实例
    """

    _all_mgrs = {}
    _update_hooks = []

    def __init__(self, region: str, sources: List[RegionMasterDbSource], version_update_interval: timedelta):
        self.region = region
        self.sources = sources
        self.latest_source = None
        self.version_update_interval = version_update_interval
        self.version_update_time = None
        self._update_task: asyncio.Task | None = None

    # 同一区服的版本刷新合并为一个共享任务。
    async def update(self):
        """同一区服共享正在执行的版本刷新；单个等待者取消不终止其他等待者。"""
        task = self._update_task
        if task is None:
            # 创建与登记之间没有 await，并发调用在同一事件循环中只会创建一个任务。
            task = asyncio.create_task(self._update_sources())
            self._update_task = task
            task.add_done_callback(self._finish_update)
        await asyncio.shield(task)

    def _finish_update(self, task: asyncio.Task):
        """刷新结束后释放在途任务，并收取无人等待时的异常，允许后续正常重试。"""
        if self._update_task is task:
            self._update_task = None
        if not task.cancelled():
            task.exception()

    async def _update_sources(self):
        """刷新各数据源版本，选择最新来源并在版本变化时启动更新回调。"""
        # logger.info(f"开始更新 {self.region} 的 {len(self.sources)} 个 MasterDB 的版本信息")
        last_version = self.latest_source.version if self.latest_source else DEFAULT_VERSION
        last_asset_version = self.latest_source.asset_version if self.latest_source else DEFAULT_VERSION
        await asyncio.gather(*[source.update_version() for source in self.sources])
        self.sources.sort(key=lambda x: get_version_order(x.version), reverse=True)
        self.latest_source = self.sources[0]
        self.version_update_time = datetime.now()
        if last_version != DEFAULT_VERSION and last_version != self.latest_source.version:
            logger.info(f"获取到最新版本的 MasterDB [{self.region}.{self.latest_source.name}] 版本为 {self.latest_source.version}")
            for hook in self._update_hooks:
                asyncio.create_task(hook(
                    self.region, self.latest_source.name,
                    self.latest_source.version, last_version,
                    self.latest_source.asset_version, last_asset_version
                ))
    
    async def get_latest_source(self) -> RegionMasterDbSource:
        """
        获取最新的MasterDB
        """
        if not self.latest_source or datetime.now() - self.version_update_time > self.version_update_interval:
            await self.update()
        return self.latest_source

    async def get_all_sources(self, force_update=False) -> List[RegionMasterDbSource]:
        """
        获取所有MasterDB数据源
        """
        if force_update or not self.latest_source or datetime.now() - self.version_update_time > self.version_update_interval:
            await self.update()
        return self.sources

    @classmethod
    def on_update(cls):
        """
        注册更新后回调装饰器
        """
        def _wrapper(func):
            cls._update_hooks.append(func)
            return func
        return _wrapper
        
    @classmethod
    def get(cls, region: str) -> "RegionMasterDbManager":
        if region not in cls._all_mgrs:
            # 从本地配置中获取
            config = asset_config.get_all()
            assert region in config and 'masterdata' in config[region], f"未找到 {region} 的 MasterData 配置"
            region_config = config[region]['masterdata']
            cls._all_mgrs[region] = RegionMasterDbManager(
                region=region, 
                sources=[RegionMasterDbSource(**source) for source in region_config["sources"]],
                version_update_interval=timedelta(minutes=region_config.get("version_update_interval", 10))
            )
        return cls._all_mgrs[region]
            
class MasterDataManager:
    """
    MasterData管理器，管理多个服务器的同一个MasterData资源
    使用 ```get(name)``` 方法获取对应资源的实例
    """
    _all_mgrs = {}

    def __init__(self, name: str, build_indices: Optional[bool] = None):
        self.name = name
        self.version = {}
        self.data: Dict[str, Any] = {}
        self.update_hooks = []
        self.map_fn = {}
        self.download_fn = {}
        self._set_index_keys(DEFAULT_INDEX_KEYS)
        self.indexed_data: Dict[str, Dict[str, Dict[str, Any]]] = {}    # indexed_data[region]['id'][id] = [item1, item2, ...]
        self._set_sort_keys(DEFAULT_SORT_KEYS)
        self.sorted_data: Dict[str, Dict[str, List[Any]]] = {}          # sorted_data[region][key] = [item1, item2, ...]
        self.lock = asyncio.Lock()
        # 内存更新可先于文件保存完成，文件版本和签名单独记录已就绪的磁盘内容。
        self.file_version: Dict[str, str] = {}
        self._file_signatures: dict[str, tuple[int, int]] = {}
        # 快照依次保存转换后数据、索引、排序结果和版本。
        self._snapshots: dict[str, tuple] = {}
        # raw_data 为 None 表示原始文件已保存，只剩版本与文件凭据需要登记。
        self._pending_cache: dict[str, dict] = {}
        # 按区服和阶段记录目标版本、连续失败次数及单调时钟重试时间。
        self._retry_state: dict[tuple[str, str], tuple[str, int, float]] = {}
        self._cache_checked: set[str] = set()
        self._update_tasks: dict[str, asyncio.Task] = {}

    def _set_index_keys(self, index_keys: Union[str, List[str], Dict[str, List[str]]]):
        if isinstance(index_keys, str):
            index_keys = [index_keys]
        if isinstance(index_keys, list):
            index_keys = {region: index_keys for region in ALL_SERVER_REGIONS}
        self.index_keys = index_keys

    def _set_sort_keys(self, sort_keys: Union[str, List[str], Dict[str, List[str]]]):
        if isinstance(sort_keys, str):
            sort_keys = [sort_keys]
        if isinstance(sort_keys, list):
            sort_keys = {region: sort_keys for region in ALL_SERVER_REGIONS}
        self.sort_keys = sort_keys

    def get_cache_path(self, region: str) -> str:
        create_folder(pjoin(MASTER_DB_CACHE_DIR, region))
        return pjoin(MASTER_DB_CACHE_DIR, region, f"{self.name}.json")

    # 候选数据准备与内存切换。

    def _prepare_candidate(self, region: str, raw_data, version: str, source_identity: str | None = None):
        """转换候选数据并建立索引和排序结果，保持当前生效数据不变。

        返回 (转换后数据, 索引, 排序结果, 版本)，转换结果为 None 时抛出 ValueError。
        """
        map_fn = self.map_fn.get('all', self.map_fn.get(region))
        data = raw_data
        if map_fn:
            # 部分映射会原地修改记录；磁盘及待保存状态必须仍持有上游原始格式。
            data = deepcopy(raw_data)
            if self.name == "costume3ds" and region in COMPACT_DATA_REGIONS:
                data = map_fn(data, version=version,
                              source_identity=source_identity or f'candidate:{id(raw_data)}')
            else:
                data = map_fn(data)
        if data is None:
            raise ValueError("MasterData 转换结果为空对象")
        indexed, sorted_data = {}, {}
        if isinstance(data, list):
            for key in self.index_keys.get(region, []):
                index = {}
                for item in data:
                    if key in item:
                        index.setdefault(item[key], []).append(item)
                indexed[key] = index
            for key in self.sort_keys.get(region, []):
                sorted_data[key] = sorted(data, key=lambda item: item.get(key))
        return data, indexed, sorted_data, version

    def _publish_candidate(self, region: str, snapshot: tuple):
        """整体切换指定区服的数据、索引、排序结果和内存版本。"""
        # 赋值之间没有 await，同一事件循环中的读取不会遇到半更新状态。
        # 线程中的参照读取使用单个快照引用。
        self._snapshots[region] = snapshot
        self.data[region], self.indexed_data[region], self.sorted_data[region], self.version[region] = snapshot

    def _retry_allowed(self, region: str, stage: str, version: str) -> bool:
        """判断指定阶段和目标版本是否已到重试时间，版本变化时立即允许尝试。"""
        failure = self._retry_state.get((region, stage))
        return failure is None or failure[0] != version or time.monotonic() >= failure[2]

    def _record_failure(self, region: str, stage: str, version: str, exc: Exception):
        """每个区服、阶段只保留当前版本的退避状态，等待下一次访问触发重试。"""
        previous = self._retry_state.get((region, stage))
        count = previous[1] + 1 if previous and previous[0] == version else 1
        delay = MASTERDATA_RETRY_SECONDS[min(count - 1, len(MASTERDATA_RETRY_SECONDS) - 1)]
        self._retry_state[region, stage] = (version, count, time.monotonic() + delay)
        logger.warning(
            f"MasterData [{region}.{self.name}] {stage}失败，{delay}秒后允许重试: {get_exc_desc(exc)}"
        )

    async def _load_from_cache(self, region: str, minimum_version: str | None = None):
        """读取缓存并准备完整快照，发布内存数据和已就绪文件状态。

        文件凭据缺失或不匹配时仍加载缓存，但版本标为未确认。
        指定 minimum_version 且文件落后时，只登记文件状态并返回 False，
        由刷新流程优先准备新版；完成本地快照发布时返回 True。
        """
        cache_path = self.get_cache_path(region)
        versions = file_db.get_copy("master_data_cache_versions", {}).get(region, {})
        receipt = file_db.get_copy("master_data_cache_files", {}).get(region, {}).get(self.name, {})

        def load_candidate():
            """读取稳定的缓存文件，核对版本凭据并返回候选快照和文件签名。"""
            before = os.stat(cache_path)
            signature = (before.st_size, before.st_mtime_ns)
            recorded_version = versions.get(self.name, DEFAULT_VERSION)
            confirmed = (
                receipt.get("version") == recorded_version
                and signature == (receipt.get("size"), receipt.get("mtime_ns"))
            )
            version = recorded_version if confirmed else DEFAULT_VERSION
            if minimum_version and get_version_order(version) < get_version_order(minimum_version):
                return None, signature, version
            raw_data = load_json(cache_path)
            after = os.stat(cache_path)
            if signature != (after.st_size, after.st_mtime_ns):
                raise RuntimeError("MasterData 文件在读取期间发生变化")
            identity = f'file:{signature[0]}:{signature[1]}'
            return self._prepare_candidate(region, raw_data, version, identity), signature, version

        started = time.perf_counter()
        snapshot, signature, file_version = await run_in_pool(load_candidate)
        record_asset_timing('cache_load', f'{region}.{self.name}', time.perf_counter()-started)
        self.file_version[region] = file_version
        self._file_signatures[region] = signature
        if snapshot is None:
            return False
        self._publish_candidate(region, snapshot)
        logger.info(f"MasterData [{region}.{self.name}] 本地数据已加载，确认版本 {snapshot[3]}")
        return True

    # 原始文件保存与版本确认。

    async def _persist_pending_cache(self, region: str):
        """保存待处理的原始文件和版本凭据，成功后清除待保存状态。

        原始文件保存成功后即可供文件读取方使用，版本凭据另行登记。
        保存失败时记录重试时间，保留已生效的内存数据和未完成步骤。
        """
        pending = self._pending_cache.get(region)
        if pending is None or not self._retry_allowed(region, "保存", pending["version"]):
            return
        version = pending["version"]
        started = time.perf_counter()
        try:
            if pending["raw_data"] is not None:
                def save_raw():
                    """原子保存上游原始数据，返回文件大小和纳秒修改时间。"""
                    path = self.get_cache_path(region)
                    dump_json(pending["raw_data"], path)
                    stat = os.stat(path)
                    return stat.st_size, stat.st_mtime_ns

                signature = await run_in_pool(save_raw)
                self.file_version[region] = version
                self._file_signatures[region] = signature
                pending["signature"] = signature
                if self.name == 'costume3ds' and region in COMPACT_DATA_REGIONS:
                    await run_in_pool(persist_costume3ds_assetbundle_patch, region, version,
                                      f"candidate:{id(pending['raw_data'])}", signature)
                pending["raw_data"] = None

            # 不同表同时完成下载时必须合并最新登记，避免整份版本字典相互覆盖。
            async with _masterdata_cache_metadata_lock:
                versions = file_db.get_copy("master_data_cache_versions", {})
                receipts = file_db.get_copy("master_data_cache_files", {})
                versions.setdefault(region, {})[self.name] = version
                size, mtime_ns = pending["signature"]
                receipts.setdefault(region, {})[self.name] = {
                    "version": version, "size": size, "mtime_ns": mtime_ns,
                }
                await run_in_pool(file_db.set_many_checked, {
                    "master_data_cache_versions": versions,
                    "master_data_cache_files": receipts,
                })
            self._pending_cache.pop(region, None)
            self._retry_state.pop((region, "保存"), None)
            logger.info(f"MasterData [{region}.{self.name}] 文件与版本已保存: {version}")
            record_asset_timing('save', f'{region}.{self.name}', time.perf_counter()-started)
        except Exception as exc:
            self._record_failure(region, "保存", version, exc)

    async def _download_from_db(self, region: str, source: RegionMasterDbSource):
        """下载并更新内存中的 MasterData，登记待保存文件并执行更新回调。"""
        # 固定本次下载对应的版本，避免来源刷新后给候选数据标记其他版本。
        version = str(source.version)
        base_url = source.base_url.rstrip('/') + '/'
        url = f"{base_url}{self.name}.json"
        download_fn = self.download_fn.get('all', self.download_fn.get(region))
        timeout = asset_config.get('default_masterdata_download_timeout')
        timings = {}
        async with asset_request_slot():
            request = (download_fn(base_url) if download_fn else
                       download_json(url, decode_in_pool=True, timings=timings))
            raw_data = await asyncio.wait_for(request, timeout)
        for stage, seconds in timings.items():
            record_asset_timing(stage, f'{region}.{self.name}', seconds)
        prepared = time.perf_counter()
        snapshot = await run_in_pool(self._prepare_candidate, region, raw_data, version)
        record_asset_timing('prepare', f'{region}.{self.name}', time.perf_counter()-prepared)
        self._publish_candidate(region, snapshot)
        self._pending_cache[region] = {"version": version, "raw_data": raw_data}
        self._retry_state.pop((region, "更新"), None)
        logger.info(f"MasterData [{region}.{self.name}] 内存数据已生效: {version}")

        # 数据切换后执行派生回调，回调失败时保留已生效数据。
        # 文件保存重试跳过这些回调。
        for name, hook, regions in self.update_hooks:
            if regions != 'all' and region not in regions:
                continue
            try:
                if asyncio.iscoroutinefunction(hook):
                    await hook(region)
                else:
                    await run_in_pool(hook, region)
                logger.info(f"MasterData [{region}.{self.name}] 更新后回调 [{name}] 执行成功")
            except Exception as e:
                logger.print_exc(f"MasterData [{region}.{self.name}] 更新后回调 [{name}] 执行失败")

    # 访问触发的更新、保存重试与并发合并。

    async def _refresh(self, region: str, source=None):
        """按需加载缓存、更新来源数据并尝试保存待处理文件。

        刷新过程由共享任务持有锁，调用方通过 shield 隔离等待者取消。
        更新失败时继续提供已有内存数据，无可用数据时抛出异常。
        """
        async with self.lock:
            if source is None and self._retry_allowed(region, "版本检查", DEFAULT_VERSION):
                try:
                    source = await RegionMasterDbManager.get(region).get_latest_source()
                    self._retry_state.pop((region, "版本检查"), None)
                except Exception as exc:
                    self._record_failure(region, "版本检查", DEFAULT_VERSION, exc)
            cache_deferred = False
            if region not in self._cache_checked:
                self._cache_checked.add(region)
                minimum = (str(source.version) if source is not None and
                           self._retry_allowed(region, '更新', str(source.version)) else None)
                try:
                    cache_deferred = not await self._load_from_cache(region, minimum)
                except Exception as exc:
                    logger.warning(f"MasterData [{region}.{self.name}] 本地缓存加载失败: {get_exc_desc(exc)}")
            if source is not None:
                version = str(source.version)
                outdated = get_version_order(self.version.get(region, DEFAULT_VERSION)) < get_version_order(version)
                if (outdated or region not in self.data) and self._retry_allowed(region, "更新", version):
                    try:
                        await self._download_from_db(region, source)
                    except Exception as exc:
                        self._record_failure(region, "更新", version, exc)
            if cache_deferred and region not in self.data:
                try:
                    await self._load_from_cache(region)
                except Exception as exc:
                    logger.warning(f"MasterData [{region}.{self.name}] 本地缓存回退失败: {get_exc_desc(exc)}")
            await self._persist_pending_cache(region)
            if self.data.get(region) is None:
                raise Exception(f"获取 MasterData [{region}.{self.name}] 的数据失败")

    def _finish_refresh(self, region: str, task: asyncio.Task):
        """收取无人等待时的异常，并释放已完成的共享任务。"""
        if self._update_tasks.get(region) is task:
            self._update_tasks.pop(region, None)
        if not task.cancelled():
            task.exception()

    async def _update_before_get(self, region: str):
        """准备读取所需数据，将同一区服的更新请求合并到共享任务。

        已有数据且处于失败等待期时直接复用。等待者取消后共享刷新继续执行。
        无可用数据且本轮无法更新时抛出异常。
        """
        task = self._update_tasks.get(region)
        source = None
        if task is None and region in self._cache_checked:
            if self._retry_allowed(region, "版本检查", DEFAULT_VERSION):
                try:
                    source = await RegionMasterDbManager.get(region).get_latest_source()
                    self._retry_state.pop((region, "版本检查"), None)
                except Exception as exc:
                    self._record_failure(region, "版本检查", DEFAULT_VERSION, exc)
            version = str(source.version) if source is not None else DEFAULT_VERSION
            needs_update = (
                source is not None
                and (region not in self.data or get_version_order(self.version[region]) < get_version_order(version))
                and self._retry_allowed(region, "更新", version)
            )
            pending = self._pending_cache.get(region)
            needs_save = pending is not None and self._retry_allowed(region, "保存", pending["version"])
            if not needs_update and not needs_save:
                if region not in self.data:
                    raise Exception(f"获取 MasterData [{region}.{self.name}] 的数据失败")
                return
        # 来源检查可能让出事件循环，创建前必须再次检查是否已有同键任务。
        task = self._update_tasks.get(region)
        if task is None:
            task = asyncio.create_task(self._refresh(region, source))
            self._update_tasks[region] = task
            task.add_done_callback(lambda completed: self._finish_refresh(region, completed))
        await asyncio.shield(task)

    async def get_data(self, region: str):
        """
        获取数据
        """
        await self._update_before_get(region)
        return self.data[region]
    
    async def get_path(self, region: str):
        """返回签名已确认的可读数据文件路径。

        内存与文件可以处于不同版本。首次更新若仅内存可用、文件尚未就绪，
        或文件已被外部替换，则抛出文件访问异常或 RuntimeError。
        """
        await self._update_before_get(region)
        path = self.get_cache_path(region)
        stat = os.stat(path)
        if (stat.st_size, stat.st_mtime_ns) != self._file_signatures.get(region):
            raise RuntimeError(f"MasterData [{region}.{self.name}] 没有已确认可读的文件")
        return path
    
    async def get_indexed(self, region: str, key: str) -> Dict[str, Any]:
        """
        获取索引，如果key没有索引，返回None
        """
        await self._update_before_get(region)
        return self.indexed_data.get(region, {}).get(key)
    
    async def get_sorted(self, region: str, key: str) -> List[Any]:
        """
        获取排序后的数据，如果key没有排序，返回None
        """
        await self._update_before_get(region)
        return self.sorted_data.get(region, {}).get(key)

    @classmethod
    def get(cls, name: str) -> "MasterDataManager":
        if name not in cls._all_mgrs:
            cls._all_mgrs[name] = MasterDataManager(name)
        return cls._all_mgrs[name]

    @classmethod
    def register_updated_hook(cls, name: str, hook_name: str, hook, regions='all'):
        """
        注册更新后回调
        """
        cls.get(name).update_hooks.append((hook_name, hook, regions))

    @classmethod
    def updated_hook(cls, name: str, hook_name: str, regions='all'):
        """
        更新后回调装饰器
        """
        def _wrapper(func):
            cls.register_updated_hook(name, hook_name, func, regions)
            return func
        return _wrapper

    @classmethod
    def register_map_fn(cls, name: str, map_fn, regions='all'):
        """
        注册映射函数
        """
        if isinstance(regions, str):
            regions = [regions]
        for region in regions:
            cls.get(name).map_fn[region] = map_fn

    @classmethod
    def map_function(cls, name: str, regions='all'):
        """
        映射函数装饰器
        """
        def _wrapper(func):
            cls.register_map_fn(name, func, regions)
            return func
        return _wrapper

    @classmethod
    def register_download_fn(cls, name: str, download_fn, regions='all'):
        """
        注册下载函数
        """
        if isinstance(regions, str):
            regions = [regions]
        for region in regions:
            cls.get(name).download_fn[region] = download_fn
    
    @classmethod
    def download_function(cls, name: str, regions='all'):
        """
        下载函数装饰器
        """
        def _wrapper(func):
            cls.register_download_fn(name, func, regions)
            return func
        return _wrapper

    @classmethod
    def set_index_keys(cls, name: str, index_keys: Union[str, List[str], Dict[str, List[str]]]):
        """
        设置索引键
        """
        cls.get(name)._set_index_keys(index_keys)

    @classmethod
    def set_sort_keys(cls, name: str, sort_keys: Union[str, List[str], Dict[str, List[str]]]):
        """
        设置排序键
        """
        cls.get(name)._set_sort_keys(sort_keys)


class RegionMasterDataWrapper:
    """
    特定区服的MasterData资源包装器
    """
    def __init__(self, region: str, name: str):
        self.region = region
        self.mgr = MasterDataManager.get(name)
    
    async def get(self):
        """
        获取数据
        """
        return await self.mgr.get_data(self.region)
    
    async def get_path(self):
        """
        获取数据文件路径
        """
        return await self.mgr.get_path(self.region)

    async def get_indexed(self, key: str):
        return await self.mgr.get_indexed(self.region, key)
    
    async def get_sorted(self, key: str):
        return await self.mgr.get_sorted(self.region, key)

    async def find_by(self, key: str, value: Any, mode='first'):
        """
        查找item[key]=value的元素，mode=first/last/all
        """
        # 使用indices优化
        ind = await self.get_indexed(key)
        if ind is not None:
            ret = ind.get(value)
            if not ret: 
                if mode == 'all': return []
                else: return None
            if mode == 'first': return ret[0]
            if mode == 'last':  return ret[-1]
            if mode == 'all': return ret
            raise ValueError(f"未知的查找模式: {mode}")
        # 没有索引的情况下遍历查找
        data = await self.get()
        return find_by(data, key, value, mode)

    async def collect_by(self, key: str, values: Union[List[Any], Set[Any]]):
        """
        收集item[key]在values中的所有元素
        """
        # 使用索引
        ind = await self.get_indexed(key)
        if ind is not None:
            ret = []
            for value in values:
                if value in ind:
                    ret.extend(ind[value])
            return ret
        # 没有索引
        data = await self.get()
        values_set = set(values)
        ret = []
        for item in data:
            if item[key] in values_set:
                ret.append(item)
        return ret
                    
    async def find_by_id(self, id: int):
        """
        查找id对应的元素
        """
        return await self.find_by('id', id)
    
    async def collect_by_ids(self, ids: Union[List[int], Set[int]]):
        """
        收集id在ids中的所有元素
        """
        return await self.collect_by('id', ids)

class RegionMasterDataCollection:
    """
    所有的MasterData资源集合
    """
    def __init__(self, region: str):
        self._region = region

        self.musics                                                         = RegionMasterDataWrapper(region, "musics")
        self.music_categories                                               = RegionMasterDataWrapper(region, "musicCategories")
        self.music_diffs                                                    = RegionMasterDataWrapper(region, "musicDifficulties")
        self.vlives                                                         = RegionMasterDataWrapper(region, "virtualLives")
        self.events                                                         = RegionMasterDataWrapper(region, "events")
        self.event_stories                                                  = RegionMasterDataWrapper(region, "eventStories")
        self.event_story_units                                              = RegionMasterDataWrapper(region, "eventStoryUnits")
        self.game_characters                                                = RegionMasterDataWrapper(region, "gameCharacters")
        self.characters_2ds                                                 = RegionMasterDataWrapper(region, "character2ds")
        self.stamps                                                         = RegionMasterDataWrapper(region, "stamps")
        self.cards                                                          = RegionMasterDataWrapper(region, "cards")
        self.card_supplies                                                  = RegionMasterDataWrapper(region, "cardSupplies")
        self.skills                                                         = RegionMasterDataWrapper(region, "skills")
        self.honors                                                         = RegionMasterDataWrapper(region, "honors")
        self.honor_groups                                                   = RegionMasterDataWrapper(region, "honorGroups")
        self.bonds_honnors                                                  = RegionMasterDataWrapper(region, "bondsHonors")
        self.bonds_honor_words                                              = RegionMasterDataWrapper(region, "bondsHonorWords")
        self.mysekai_materials                                              = RegionMasterDataWrapper(region, "mysekaiMaterials")
        self.mysekai_items                                                  = RegionMasterDataWrapper(region, "mysekaiItems")
        self.mysekai_fixtures                                               = RegionMasterDataWrapper(region, "mysekaiFixtures")
        self.mysekai_musicrecords                                           = RegionMasterDataWrapper(region, "mysekaiMusicRecords")
        self.mysekai_phenomenas                                             = RegionMasterDataWrapper(region, "mysekaiPhenomenas")
        self.mysekai_blueprints                                             = RegionMasterDataWrapper(region, "mysekaiBlueprints")
        self.mysekai_site_harvest_fixtures                                  = RegionMasterDataWrapper(region, "mysekaiSiteHarvestFixtures")
        self.mysekai_fixture_maingenres                                     = RegionMasterDataWrapper(region, "mysekaiFixtureMainGenres")
        self.mysekai_fixture_subgenres                                      = RegionMasterDataWrapper(region, "mysekaiFixtureSubGenres")
        self.mysekai_character_talks                                        = RegionMasterDataWrapper(region, "mysekaiCharacterTalks")
        self.mysekai_game_character_unit_groups                             = RegionMasterDataWrapper(region, "mysekaiGameCharacterUnitGroups")
        self.game_character_units                                           = RegionMasterDataWrapper(region, "gameCharacterUnits")
        self.mysekai_material_chara_relations                               = RegionMasterDataWrapper(region, "mysekaiMaterialGameCharacterRelations")
        self.resource_boxes                                                 = RegionMasterDataWrapper(region, "resourceBoxes")
        self.boost_items                                                    = RegionMasterDataWrapper(region, "boostItems")
        self.materials                                                      = RegionMasterDataWrapper(region, "materials")
        self.mysekai_fixture_tags                                           = RegionMasterDataWrapper(region, "mysekaiFixtureTags")
        self.mysekai_blueprint_material_cost                                = RegionMasterDataWrapper(region, "mysekaiBlueprintMysekaiMaterialCosts")
        self.mysekai_fixture_only_disassemble_materials                     = RegionMasterDataWrapper(region, "mysekaiFixtureOnlyDisassembleMaterials")
        self.music_vocals                                                   = RegionMasterDataWrapper(region, "musicVocals")
        self.outside_characters                                             = RegionMasterDataWrapper(region, "outsideCharacters")
        self.mysekai_gate_levels                                            = RegionMasterDataWrapper(region, "mysekaiGateLevels")
        self.myskeia_gate_material_groups                                   = RegionMasterDataWrapper(region, "mysekaiGateMaterialGroups")
        self.event_story_units                                              = RegionMasterDataWrapper(region, "eventStoryUnits")
        self.card_episodes                                                  = RegionMasterDataWrapper(region, "cardEpisodes")
        self.event_cards                                                    = RegionMasterDataWrapper(region, "eventCards")
        self.event_musics                                                   = RegionMasterDataWrapper(region, "eventMusics")
        self.costume3ds                                                     = RegionMasterDataWrapper(region, "costume3ds")
        self.card_costume3ds                                                = RegionMasterDataWrapper(region, "cardCostume3ds")
        self.ng_words                                                       = RegionMasterDataWrapper(region, "ngWords")
        self.mysekai_gate_material_groups                                   = RegionMasterDataWrapper(region, "mysekaiGateMaterialGroups")
        self.world_blooms                                                   = RegionMasterDataWrapper(region, "worldBlooms")
        self.gachas                                                         = RegionMasterDataWrapper(region, "gachas")
        self.area_item_levels                                               = RegionMasterDataWrapper(region, "areaItemLevels")
        self.area_items                                                     = RegionMasterDataWrapper(region, "areaItems")
        self.areas                                                          = RegionMasterDataWrapper(region, "areas")
        self.card_rarities                                                  = RegionMasterDataWrapper(region, "cardRarities")
        self.character_ranks                                                = RegionMasterDataWrapper(region, "characterRanks")
        self.event_deck_bonuses                                             = RegionMasterDataWrapper(region, "eventDeckBonuses")
        self.event_exchange_summaries                                       = RegionMasterDataWrapper(region, "eventExchangeSummaries")
        self.event_items                                                    = RegionMasterDataWrapper(region, "eventItems")
        self.event_rarity_bonus_rates                                       = RegionMasterDataWrapper(region, "eventRarityBonusRates")
        self.master_lessons                                                 = RegionMasterDataWrapper(region, "masterLessons")
        self.shop_items                                                     = RegionMasterDataWrapper(region, "shopItems")
        self.world_bloom_different_attribute_bonuses                        = RegionMasterDataWrapper(region, "worldBloomDifferentAttributeBonuses")
        self.world_bloom_support_deck_bonuses                               = RegionMasterDataWrapper(region, "worldBloomSupportDeckBonuses")
        self.world_bloom_support_deck_unit_event_limited_bonuses            = RegionMasterDataWrapper(region, "worldBloomSupportDeckUnitEventLimitedBonuses")
        self.ingame_combos                                                  = RegionMasterDataWrapper(region, "ingameCombos")
        self.ingame_notes                                                   = RegionMasterDataWrapper(region, "ingameNotes")
        # 终章表需显式注册并由组卡同步调用，单靠上游版本更新不会自动下载新表。
        self.event_card_bonus_limits                                        = RegionMasterDataWrapper(region, "eventCardBonusLimits")
        self.event_honor_bonuses                                             = RegionMasterDataWrapper(region, "eventHonorBonuses")
        self.event_mysekai_fixture_game_character_performance_bonus_limits   = RegionMasterDataWrapper(region, "eventMysekaiFixtureGameCharacterPerformanceBonusLimits")
        self.event_skill_score_up_limits                                    = RegionMasterDataWrapper(region, "eventSkillScoreUpLimits")
        self.card_mysekai_canvas_bonuses                                    = RegionMasterDataWrapper(region, "cardMysekaiCanvasBonuses")
        self.mysekai_fixture_game_character_groups                          = RegionMasterDataWrapper(region, "mysekaiFixtureGameCharacterGroups")
        self.mysekai_fixture_game_character_group_performance_bonuses       = RegionMasterDataWrapper(region, "mysekaiFixtureGameCharacterGroupPerformanceBonuses")
        self.mysekai_gates                                                  = RegionMasterDataWrapper(region, "mysekaiGates")
        self.mysekai_character_talk_fixture_common_mysekai_fixture_groups   = RegionMasterDataWrapper(region, "mysekaiCharacterTalkFixtureCommonMysekaiFixtureGroups")
        self.mysekai_character_talk_fixture_commons                         = RegionMasterDataWrapper(region, "mysekaiCharacterTalkFixtureCommons")
        self.mysekai_character_talks                                        = RegionMasterDataWrapper(region, "mysekaiCharacterTalks")
        self.mysekai_character_talk_condition_groups                        = RegionMasterDataWrapper(region, "mysekaiCharacterTalkConditionGroups")
        self.mysekai_character_talk_conditions                              = RegionMasterDataWrapper(region, "mysekaiCharacterTalkConditions")
        self.character_archive_mysekai_character_talk_groups                = RegionMasterDataWrapper(region, "characterArchiveMysekaiCharacterTalkGroups")
        self.mysekai_musicrecord_categories                                 = RegionMasterDataWrapper(region, "mysekaiMusicRecordCategories")
        self.music_tags                                                     = RegionMasterDataWrapper(region, "musicTags")
        self.mysekai_gate_character_lotteries                               = RegionMasterDataWrapper(region, "mysekaiGateCharacterLotteries")
        self.cheerful_carnival_teams                                        = RegionMasterDataWrapper(region, "cheerfulCarnivalTeams")
        self.challenge_live_high_score_rewards                              = RegionMasterDataWrapper(region, "challengeLiveHighScoreRewards")
        self.mysekai_phenomena_background_colors                            = RegionMasterDataWrapper(region, "mysekaiPhenomenaBackgroundColors")
        self.gacha_ceil_items                                               = RegionMasterDataWrapper(region, "gachaCeilItems")
        self.gacha_tickets                                                  = RegionMasterDataWrapper(region, "gachaTickets")
        self.player_frames                                                  = RegionMasterDataWrapper(region, "playerFrames")
        self.player_frame_groups                                            = RegionMasterDataWrapper(region, "playerFrameGroups")
        self.limited_time_musics                                            = RegionMasterDataWrapper(region, "limitedTimeMusics")
        self.bonds                                                          = RegionMasterDataWrapper(region, "bonds")
        self.levels                                                         = RegionMasterDataWrapper(region, "levels")
        self.character_mission_v2_parameter_groups                          = RegionMasterDataWrapper(region, "characterMissionV2ParameterGroups")

    async def get(self, name: str):
        wrapper = RegionMasterDataWrapper(self._region, name)
        return await wrapper.get()
    
    async def get_version(self) -> str:
        mgr = RegionMasterDbManager.get(self._region)
        return (await mgr.get_latest_source()).version


# ================================ MasterData自定义索引 ================================ #

MasterDataManager.set_index_keys("cards", ['id', 'characterId'])
MasterDataManager.set_index_keys("eventStories", ['id', 'bannerGameCharacterUnitId', 'eventId'])
MasterDataManager.set_index_keys("mysekaiBlueprints", ['id', 'craftTargetId'])
MasterDataManager.set_index_keys("mysekaiBlueprintMysekaiMaterialCosts", ['id', 'mysekaiBlueprintId'])
MasterDataManager.set_index_keys("mysekaiFixtureOnlyDisassembleMaterials", ['id', 'mysekaiFixtureId'])
MasterDataManager.set_index_keys("cardCostume3ds", ['cardId'])
MasterDataManager.set_index_keys("mysekaiCharacterTalkFixtureCommonMysekaiFixtureGroups", ['mysekaiFixtureId', 'groupId'])
MasterDataManager.set_index_keys("mysekaiCharacterTalkFixtureCommons", ['mysekaiCharacterTalkFixtureCommonMysekaiFixtureGroupId', 'gameCharacterUnitId', 'id'])
MasterDataManager.set_index_keys("mysekaiCharacterTalks", ['id', 'mysekaiCharacterTalkConditionGroupId'])
MasterDataManager.set_index_keys("mysekaiCharacterTalkConditionGroups", ['groupId', 'mysekaiCharacterTalkConditionId'])
MasterDataManager.set_index_keys("mysekaiCharacterTalkConditions", ['id', 'mysekaiCharacterTalkConditionType'])
MasterDataManager.set_index_keys("mysekaiMusicRecords", ['id', 'externalId'])
MasterDataManager.set_index_keys("musicTags", ['id', 'musicId'])
MasterDataManager.set_index_keys("musicCategories", ['id', 'musicId'])
MasterDataManager.set_index_keys("eventDeckBonuses", ['id', 'eventId'])
MasterDataManager.set_index_keys("eventCards", ['id', 'eventId'])
MasterDataManager.set_index_keys("musicDifficulties", ['id', 'musicId'])
MasterDataManager.set_index_keys("cardEpisodes", ['id', 'cardId'])
MasterDataManager.set_index_keys("shopItems", ['id', 'resourceBoxId'])
MasterDataManager.set_index_keys("areaItemLevels", ['areaItemId'])
MasterDataManager.set_index_keys("limitedTimeMusics", ['id', 'musicId'])
MasterDataManager.set_index_keys("levels", ['id', 'levelType'])
MasterDataManager.set_index_keys("eventMusics", ['id', 'eventId'])

MasterDataManager.set_sort_keys('events', ['startAt'])

# ================================ MasterData自定义下载 ================================ #

COMPACT_DATA_REGIONS = ['kr', 'cn', 'tw']

# 为 compact 区服注册 costume3ds 的专用映射：
# 在数据加载阶段完成字段归一化和补丁补全，业务层按正常字段读取即可。
for compact_region in COMPACT_DATA_REGIONS:
    MasterDataManager.register_map_fn("costume3ds", make_costume3ds_map_fn(compact_region), compact_region)

def convert_compact_data(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    enums = data.get('__ENUM__', {})
    ret = []
    for key, val in data.items():
        if key.startswith("__"): continue
        if not ret:
            ret = [{} for _ in range(len(val))]
        use_enum = key in enums
        for x, item in zip(val, ret):
            if x is not None:
                if use_enum:
                    x = enums[key][x]
                item[key] = x
    return ret
            
@MasterDataManager.download_function("resourceBoxes", regions=COMPACT_DATA_REGIONS)
async def resource_boxes_download_fn(base_url):
    # resbox = await download_json(f"{base_url}/compactResourceBoxes.json")
    # resbox_detail = await download_json(f"{base_url}/compactResourceBoxDetails.json")
    box_timings, detail_timings = {}, {}
    resbox = await download_json(f"{base_url}/resourceBoxes.json", decode_in_pool=True, timings=box_timings)
    resbox_detail = await download_json(f"{base_url}/resourceBoxDetails.json", decode_in_pool=True, timings=detail_timings)
    for name, timings in (('resourceBoxes', box_timings), ('resourceBoxDetails', detail_timings)):
        for stage, seconds in timings.items():
            record_asset_timing(stage, name, seconds)
    def convert(resbox, resbox_detail):
        # resbox = convert_compact_data(resbox)
        # resbox_detail = convert_compact_data(resbox_detail)
        details = {}
        for item in resbox_detail:
            key = f"{item['resourceBoxPurpose']}_{item['resourceBoxId']}"
            if key not in details:
                details[key] = []
            details[key].append(item)
        for item in resbox:
            key = f"{item['resourceBoxPurpose']}_{item['id']}"
            item['details'] = details.get(key, [])
        return resbox
    return await run_in_pool(convert, resbox, resbox_detail)

# @MasterDataManager.download_function("costume3ds", regions=COMPACT_DATA_REGIONS)
# async def costume3ds_download_fn(base_url):
#     costume3ds = await download_json(f"{base_url}/compactCostume3ds.json")
#     return await run_in_pool(convert_compact_data, costume3ds)


# ================================ MasterData自定义转换 ================================ #


@MasterDataManager.map_function("virtualLives")
def vlives_map_fn(vlives):
    all_ret = []
    for vlive in vlives:
        ret = {}
        ret["id"]         = vlive["id"]
        ret["name"]       = vlive["name"]
        ret["show_start"] = datetime.fromtimestamp(vlive["startAt"] / 1000)
        ret["show_end"]   = datetime.fromtimestamp(vlive["endAt"]   / 1000)
        if datetime.now() - ret["show_end"] > timedelta(days=7):
            continue
        ret["schedule"] = []
        if len(vlive.get("virtualLiveSchedules", [])) == 0:
            continue
        rest_num = 0
        for schedule in vlive["virtualLiveSchedules"]:
            start = datetime.fromtimestamp(schedule["startAt"] / 1000)
            end   = datetime.fromtimestamp(schedule["endAt"]   / 1000)
            ret["schedule"].append((start, end))
            if datetime.now() < start: rest_num += 1
        ret["current"] = None
        for start, end in ret["schedule"]:
            if datetime.now() < end:
                ret["current"] = (start, end)
                ret["living"] = datetime.now() >= start
                break
        ret["rest_num"] = rest_num
        ret["start"] = ret["schedule"][0][0]
        ret["end"]   = ret["schedule"][-1][1]
        ret['asset_name'] = vlive['assetbundleName']
        ret["rewards"] = vlive['virtualLiveRewards']
        ret["characters"] = vlive['virtualLiveCharacters']
        all_ret.append(ret)
    all_ret.sort(key=lambda x: x["start"])
    return all_ret

@MasterDataManager.map_function("resourceBoxes")
def resource_boxes_map_fn(resource_boxes):
    ret = {}    # ret[purpose][bid]=item
    for item in resource_boxes:
        purpose = item["resourceBoxPurpose"]
        bid     = item["id"]
        if purpose not in ret:
            ret[purpose] = {}
        ret[purpose][bid] = item
    return ret

@MasterDataManager.map_function("eventStoryUnits")
def event_story_units_map_fn(event_story_units):
    ret = { 
        "events": {}, 
        "banner_event_story_id_set": set(),
    }
    for item in event_story_units:
        esid = item["eventStoryId"]
        unit = item["unit"]
        relation = item['eventStoryUnitRelation']
        if esid not in ret['events']:
            ret['events'][esid] = { "main": None, "sub": [] }
        if relation == "main":
            ret['events'][esid]["main"] = unit
            ret['banner_event_story_id_set'].add(esid)
        else:
            ret['events'][esid]["sub"].append(unit)
            ret['banner_event_story_id_set'].discard(esid)
    return ret

@MasterDataManager.map_function("ngWords")
def ng_words_map_fn(ng_words):
    return set([item['word'] for item in ng_words])


# ================================ 解包Asset资源 ================================ #

DEFAULT_RIP_ASSET_DIR = f"{SEKAI_ASSET_DIR}/rip"
DEFAULT_GET_RIP_ASSET_TIMEOUT_CFG = asset_config.item('default_rip_asset_download_timeout')
RIP_IMG_CACHE_MAX_RES_CFG = asset_config.item('rip_img_cache_max_res')

ONDEMAND_PREFIXES = ['event', 'gacha', 'music/long', 'mysekai', 'virtual_live']
STARTAPP_PREFIXES = ['bonds_honor', 'honor', 'thumbnail', 'character', 'music', 'rank_live', 'stamp', 'home/banner', 'player_frame', 'areaitem']

def sekai_best_url_map(url: str) -> str:
    # 移除_rip
    url = url.replace("_rip", "")
    # 谱面文件添加.txt
    if 'music_score' in url:
        url = url + ".txt"
    return url

def haruki_url_map(url: str) -> str:
    idx = url.find("assets/")
    assert idx != -1, f"解包资源url格式错误: {url}"
    idx = idx + len("assets/")
    part1, part2 = url[:idx], url[idx:]

    # 移除_rip
    part2 = part2.replace("_rip", "")
    # 谱面文件添加.txt
    if 'music_score' in part2:
        part2 = part2 + ".txt"
    # .asset改为.json
    part2 = part2.replace(".asset", ".json")
    # 添加类别
    if any([part2.startswith(prefix) for prefix in ONDEMAND_PREFIXES]):
        category = 'ondemand'
    elif any([part2.startswith(prefix) for prefix in STARTAPP_PREFIXES]):
        category = 'startapp'
    else:
        logger.warning(f"在startapp和ondemand都找不到: {url}")
        category = 'ondemand'

    return f"{part1}{category}/{part2}"

def pjsekai_moe_url_map(url: str) -> str:
    # 移除_rip
    url = url.replace("_rip", "")
    return url

def unipjsk_url_map(url: str) -> str:
    idx = url.find("assets.unipjsk.com/")
    assert idx != -1, f"解包资源url格式错误: {url}"
    idx = idx + len("assets.unipjsk.com/")
    part1, part2 = url[:idx], url[idx:]

    # 移除_rip
    part2 = part2.replace("_rip", "")
    # 替换.asset为.json
    part2 = part2.replace(".asset", ".json")
    # 添加类别
    if any([part2.startswith(prefix) for prefix in ONDEMAND_PREFIXES]):
        category = 'ondemand'
    elif any([part2.startswith(prefix) for prefix in STARTAPP_PREFIXES]):
        category = 'startapp'
    else:
        logger.warning(f"在startapp和ondemand都找不到: {url}")
        category = 'ondemand'

    return f"{part1}{category}/{part2}"

# 预设的解包资源url映射方法
DEFAULT_URL_MAP_METHODS = {
    "sekai.best": sekai_best_url_map,
    "haruki": haruki_url_map,
    "pjsekai.moe": pjsekai_moe_url_map,
    "unipjsk": unipjsk_url_map,
}

class RegionRipAssetSource:
    """
    区服解包资源数据源
    """
    def __init__(self, name: str, base_url: str, url_map_method_name: str = None, prefixes: List[str] = None):
        self.name = name
        self.base_url = base_url
        self.url_map_method = lambda x: x
        if url_map_method_name:
            self.url_map_method = DEFAULT_URL_MAP_METHODS[url_map_method_name]
        elif self.name in DEFAULT_URL_MAP_METHODS:
            self.url_map_method = DEFAULT_URL_MAP_METHODS[self.name]
        self.prefixes = prefixes

class AssetNotFoundError(HttpError):
    """所有适用资源源均明确返回 404 时抛出的资源缺失异常。"""


class RegionRipAssetManger:
    """
    区服解包资源管理器
    使用 ```get(region)``` 方法获取对应区服的实例
    """
    _all_mgrs: dict[str, "RegionRipAssetManger"] = {}
    _img_cache_map: dict[str, Image.Image] = {}

    def __init__(self, region: str, sources: List[RegionRipAssetSource]):
        self.region = region
        self.sources = sources
        self.cache_dir = pjoin(DEFAULT_RIP_ASSET_DIR, region)
        self.cached_images: Dict[str, Image.Image] = {}
        self._asset_requests: dict[tuple, asyncio.Task] = {}
        self._missing_assets = OrderedDict()
        create_folder(self.cache_dir)

    def _iter_local_cache_paths(self, path: str) -> List[str]:
        candidates: List[str] = [path]

        # _rip -> 非 _rip
        if "_rip/" in path:
            alt_path = path.replace("_rip/", "/")
            if alt_path not in candidates:
                candidates.append(alt_path)

        # 非 _rip -> _rip（仅处理最后一层目录，适配 xxx/dir/file 与 xxx/dir_rip/file）
        parts = path.split("/")
        if len(parts) >= 2:
            parent = parts[-2]
            if not parent.endswith("_rip"):
                alt_parent = parent + "_rip"
                alt_path = "/".join(parts[:-2] + [alt_parent, parts[-1]])
                if alt_path not in candidates:
                    candidates.append(alt_path)

        return candidates

    def get_local_asset_cache_path(
        self,
        path: str,
        cache_expire_secs=None,
    ) -> Optional[str]:
        """仅查找本地区服缓存，不触发任何远程下载。"""
        for cache_rel_path in self._iter_local_cache_paths(path):
            cache_path = pjoin(self.cache_dir, cache_rel_path)
            try:
                if not os.path.exists(cache_path):
                    continue
                if cache_expire_secs is not None:
                    cache_age = datetime.now().timestamp() - os.path.getmtime(cache_path)
                    if cache_age >= cache_expire_secs:
                        continue
                return cache_path
            except OSError:
                # 缓存可能在检查期间被更新器替换，按未命中继续尝试其他路径。
                continue
        return None

    @classmethod
    def get(cls, region: str) -> "RegionRipAssetManger":
        if region not in cls._all_mgrs:
            # 从本地配置中获取
            config = asset_config.get_all()
            assert region in config and 'rip' in config[region], f"未找到 {region} 的 RipAsset 配置"
            region_config = config[region]['rip']
            cls._all_mgrs[region] = RegionRipAssetManger(
                region=region, 
                sources=[RegionRipAssetSource(**source) for source in region_config["sources"]]
            )
        return cls._all_mgrs[region]

    async def _download_data(self, url: str, timeout: int) -> bytes:
        async def do_download():
            async with get_client_session().get(url, verify_ssl=False) as resp:
                if resp.status != 200:
                    raise HttpError(resp.status, '请求失败')
                return await resp.read()
        async with asset_request_slot():
            started = time.perf_counter()
            if not timeout:
                data = await do_download()
            else:
                data = await asyncio.wait_for(do_download(), timeout)
            record_asset_timing('image_download', self.region, time.perf_counter()-started)
            return data

    async def get_asset(
        self, path: str, use_cache=True, allow_error=True, default=None,
        cache_expire_secs=None, timeout=DEFAULT_GET_RIP_ASSET_TIMEOUT_CFG,
        *, read_in_pool=False, cache_missing_secs=0,
    ) -> bytes:
        """读取资源；可选图片可合并下载并短期缓存明确的全源 404。

        cache_missing_secs 大于零时启用共享请求。等待者取消不终止下载，
        use_cache=False 绕过缺失记录以支持强制刷新。
        """
        if (not cache_missing_secs and ASSET_REQUEST_LIMIT.get() is None) or not use_cache:
            return await self._get_asset(path, use_cache, allow_error, default,
                                         cache_expire_secs, timeout, read_in_pool=read_in_pool)
        started = time.perf_counter()
        cached = (await run_in_pool_until_complete(self._read_cached_asset, path, cache_expire_secs) if read_in_pool
                  else self._read_cached_asset(path, cache_expire_secs))
        if read_in_pool:
            record_asset_timing('image_read', path, time.perf_counter()-started)
        if cached is not None:
            return cached
        sources = tuple((source.base_url.rstrip('/'), tuple(source.prefixes or ()), id(source.url_map_method))
                        for source in self.sources)
        key = (path, sources, get_cfg_or_value(timeout), cache_expire_secs)
        expires = self._missing_assets.get(key, 0)
        if expires > time.monotonic():
            if not allow_error:
                raise AssetNotFoundError(404, f'资源暂不存在: {path}')
            return default
        self._missing_assets.pop(key, None)
        task = self._asset_requests.get(key)
        if task is None:
            task = asyncio.create_task(self._get_asset(
                path, use_cache, False, None, cache_expire_secs, timeout,
                read_in_pool=read_in_pool, track_missing=True))
            self._asset_requests[key] = task

            def finish(completed):
                """下载实际结束后释放共享任务，仅将全源 404 登记为短期缺失。"""
                if self._asset_requests.get(key) is completed:
                    self._asset_requests.pop(key, None)
                if (not completed.cancelled() and isinstance(completed.exception(), AssetNotFoundError)
                        and cache_missing_secs):
                    self._missing_assets[key] = time.monotonic() + cache_missing_secs
                    self._missing_assets.move_to_end(key)
                    # 只保留最近 512 个缺失键，避免查询大量旧卡池后持续增长。
                    while len(self._missing_assets) > 512:
                        self._missing_assets.popitem(last=False)

            task.add_done_callback(finish)
        try:
            return await asyncio.shield(task)
        except Exception:
            if not allow_error:
                raise
            return default

    def _read_cached_asset(self, path, cache_expire_secs):
        """读取已存在且未过期的本地资源，未命中时返回 None。"""
        for relative in self._iter_local_cache_paths(path):
            cache_path = pjoin(self.cache_dir, relative)
            try:
                if (cache_expire_secs is not None and
                        time.time() - os.path.getmtime(cache_path) >= cache_expire_secs):
                    continue
                with open(cache_path, 'rb') as stream:
                    return stream.read()
            except OSError:
                continue
        return None

    def _write_cached_asset(self, path, data):
        """原子保存完整资源，避免并发读取遇到尚未写完的图片。"""
        create_parent_folder(path)
        temporary = f'{path}.{os.getpid()}.{threading.get_ident()}.tmp'
        try:
            with open(temporary, 'wb') as stream:
                stream.write(data)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.remove(temporary)

    async def _get_asset(
        self,
        path: str, 
        use_cache=True, 
        allow_error=True, 
        default=None, 
        cache_expire_secs=None, 
        timeout: Union[int, ConfigItem]=DEFAULT_GET_RIP_ASSET_TIMEOUT_CFG,
        *, read_in_pool=False, track_missing=False,
    ) -> bytes:
        """
        获取任意类型解包资源，返回二进制数据，参数：
        - `path`: 解包资源路径
        - `use_cache`: 是否使用缓存
        - `allow_error`: 是否允许错误
        - `default`: 允许错误的情况下的默认值
        - `cache_expire_secs`: 缓存过期时间
        - `timeout`: 超时时间
        """
        cache_path = pjoin(self.cache_dir, path)
        # 首先尝试从缓存加载
        if use_cache:
            cached = (await run_in_pool_until_complete(self._read_cached_asset, path, cache_expire_secs) if read_in_pool
                      else self._read_cached_asset(path, cache_expire_secs))
            if cached is not None:
                return cached
        
        # 尝试从网络下载
        error_list: List[Tuple[str, str]] = []
        statuses = []
        for source in self.sources:
            if source.prefixes and not any([path.startswith(prefix) for prefix in source.prefixes]):
                continue

            url = None
            try:
                if not source.base_url.endswith("/"):
                    source.base_url += "/"
                url = source.url_map_method(source.base_url + path)
                data = await self._download_data(url, get_cfg_or_value(timeout))
                if use_cache:
                    if read_in_pool:
                        await run_in_pool_until_complete(self._write_cached_asset, cache_path, data)
                    else:
                        self._write_cached_asset(cache_path, data)
                return data
            
            except Exception as e:
                statuses.append(getattr(e, 'status_code', None))
                e = get_exc_desc(e)
                error_list.append((source.name, e))
                # logger.print_exc(f"从数据源 [{source.name}] 获取 {self.region} 解包资源 {path} 失败: {e}, url={url}")
                logger.warning(f"从数据源 [{source.name}] 获取 {self.region} 解包资源 {path} 失败: {e}, url={url}")

        if track_missing and statuses and all(status == 404 for status in statuses):
            raise AssetNotFoundError(404, f'所有数据源均无此资源: {path}')
        if not allow_error:
            error_list_text = ""
            for source_name, err in error_list:
                error_list_text += f"[{source_name}] {truncate(err, 40)}\n"
            raise Exception(f"从所有数据源获取 {self.region} 解包资源 {path} 失败:\n{error_list_text.strip()}")
        
        logger.warning(f"从所有数据源获取 {self.region} 解包资源 {path} 失败: {error_list}，返回默认值")
        return default

    async def get_asset_cache_path(
        self,
        path: str, 
        allow_error=True, 
        default=None, 
        cache_expire_secs=None, 
        timeout: Union[int, ConfigItem]=DEFAULT_GET_RIP_ASSET_TIMEOUT_CFG,
    ) -> str:
        """
        获取任意类型解包资源在本地缓存的路径参数
        - `path`: 解包资源路径
        - `allow_error`: 是否允许错误
        - `default`: 允许错误的情况下的默认值
        - `cache_expire_secs`: 缓存过期时间
        - `timeout`: 超时时间
        """
        # 首先尝试从缓存加载
        cache_path = (await run_in_pool_until_complete(self.get_local_asset_cache_path, path, cache_expire_secs)
                      if ASSET_PREPARE_IN_POOL.get() else self.get_local_asset_cache_path(path, cache_expire_secs))
        if cache_path:
            return cache_path
        if ASSET_REQUEST_LIMIT.get() is not None:
            data = await self.get_asset(path, allow_error=allow_error, default=None,
                                        cache_expire_secs=cache_expire_secs, timeout=timeout,
                                        read_in_pool=True)
            return self.get_local_asset_cache_path(path, cache_expire_secs=None) if data is not None else default
        cache_path = pjoin(self.cache_dir, path)
        
        # 尝试从网络下载
        error_list: List[Tuple[str, str]] = []
        for source in self.sources:
            if source.prefixes and not any([path.startswith(prefix) for prefix in source.prefixes]):
                continue

            url = None
            try:
                if not source.base_url.endswith("/"):
                    source.base_url += "/"
                url = source.url_map_method(source.base_url + path)
                data = await self._download_data(url, get_cfg_or_value(timeout))
                create_parent_folder(cache_path)
                with open(cache_path, "wb") as f:
                    f.write(data)
                return cache_path
            
            except Exception as e:
                e = get_exc_desc(e)
                error_list.append((source.name, e))
                # logger.print_exc(f"从数据源 [{source.name}] 获取 {self.region} 解包资源 {path} 失败: {e}, url={url}")
                logger.warning(f"从数据源 [{source.name}] 获取 {self.region} 解包资源 {path} 失败: {e}, url={url}")

        if not allow_error:
            error_list_text = ""
            for source_name, err in error_list:
                error_list_text += f"[{source_name}] {truncate(err, 40)}\n"
            raise Exception(f"从所有数据源获取 {self.region} 解包资源 {path} 失败:\n{error_list_text.strip()}")
        
        logger.warning(f"从所有数据源获取 {self.region} 解包资源 {path} 失败: {error_list}，返回默认值")
        return default

    async def img(
        self,
        path: str, 
        use_cache=True, 
        allow_error=True, 
        default=UNKNOWN_IMG, 
        cache_expire_secs=None, 
        timeout: Union[int, ConfigItem]=DEFAULT_GET_RIP_ASSET_TIMEOUT_CFG,
        use_img_cache: bool=False,
        img_cache_max_res: Union[int, ConfigItem]=RIP_IMG_CACHE_MAX_RES_CFG,
        decode_in_pool: bool=False,
        cache_missing_secs: int=0,
    ) -> Image.Image:
        """
        获取图片类型解包资源，参数：
        - `path`: 解包资源路径
        - `use_cache`: 是否使用缓存
        - `allow_error`: 是否允许错误
        - `default`: 允许错误的情况下的默认值
        - `cache_expire_secs`: 缓存过期时间
        - `timeout`: 超时时间
        - `use_img_cache`: 是否使用图片缓存
        - `img_cache_max_res`: 图片缓存最大分辨率，None则不缩放
        - `decode_in_pool`: 在线程池中解码图片，减少大图解码占用事件循环的时间
        """
        decode_in_pool = decode_in_pool or ASSET_PREPARE_IN_POOL.get()
        # 尝试从图片缓存加载
        if use_img_cache and path in self.cached_images:
            return self.cached_images[path]
        data = await self.get_asset(path, use_cache, allow_error, default, cache_expire_secs,
                                    get_cfg_or_value(timeout), read_in_pool=decode_in_pool,
                                    cache_missing_secs=cache_missing_secs)
        if data is default:
            return default
        try: 
            started = time.perf_counter()
            img = await run_in_pool_until_complete(open_image, io.BytesIO(data)) if decode_in_pool else open_image(io.BytesIO(data))
            if decode_in_pool:
                record_asset_timing('image_decode', path, time.perf_counter()-started)
            if use_img_cache:
                if img_cache_max_res:
                    max_res = parse_cfg_num(get_cfg_or_value(img_cache_max_res))
                    w, h = img.size
                    if w * h > max_res:
                        scale = math.sqrt(max_res / (w * h))
                        img = resize_keep_ratio(img, scale, mode='scale')
                img_hash = get_image_pixel_hash(img)
                # 由于不同区服图片资源存在大量重复，使用全局缓存减少内存占用
                if img_hash in self._img_cache_map:
                    img = self._img_cache_map[img_hash]
                else:
                    self._img_cache_map[img_hash] = img
                self.cached_images[path] = img
            return img
        except asyncio.CancelledError:
            raise
        except: pass
        if not allow_error:
            raise Exception(f"解析下载的 {self.region} 解包资源 {path} 为图片失败")
        logger.warning(f"解析下载的 {self.region} 解包资源 {path} 为图片失败: 返回默认值")
        return default
    
    async def json(
        self,
        path: str, 
        use_cache=True, 
        allow_error=True, 
        default=None, 
        cache_expire_secs=None, 
        timeout: Union[int, ConfigItem]=DEFAULT_GET_RIP_ASSET_TIMEOUT_CFG,
    ) -> Any:
        """
        获取json类型解包资源，参数：
        - `path`: 解包资源路径
        - `use_cache`: 是否使用缓存
        - `allow_error`: 是否允许错误
        - `default`: 允许错误的情况下的默认值
        - `cache_expire_secs`: 缓存过期时间
        - `timeout`: 超时时间
        """
        in_pool = ASSET_PREPARE_IN_POOL.get()
        data = await self.get_asset(path, use_cache, allow_error, default, cache_expire_secs,
                                    get_cfg_or_value(timeout), read_in_pool=in_pool)
        try:
            return await run_in_pool_until_complete(loads_json, data) if in_pool else loads_json(data)
        except asyncio.CancelledError:
            raise
        except: pass
        if not allow_error:
            raise Exception(f"解析下载的 {self.region} 解包资源 {path} 为json失败")
        logger.warning(f"解析下载的 {self.region} 解包资源 {path} 为json失败: 返回默认值")
        return default


@repeat_with_interval(10, "解包图片缓存调试信息", logger)
def log_rip_asset_img_cache_info():
    if asset_config.get('debug_log_img_cache', False):
        total_imgs = len(RegionRipAssetManger._img_cache_map)
        total_mem = 0
        for img in RegionRipAssetManger._img_cache_map.values():
            w, h = img.size
            total_mem += w * h * 4
        logger.info(f"解包图片缓存数量: {total_imgs} 张, 估计内存占用: {total_mem / 1024 / 1024:.2f} MB") 

        
# ================================ 静态图片资源 ================================ #

DEFAULT_STATIC_IMAGE_DIR = f"{SEKAI_ASSET_DIR}/static_images"

@dataclass  
class StaticImageRes:
    def __init__(self, dir: str = None):
        self.dir = dir or DEFAULT_STATIC_IMAGE_DIR
        self.images: Dict[str, Tuple[Image.Image, int]] = {}  # path -> (image, mtime)
        self.lock = threading.Lock()

    def get(self, path: str, size: Tuple[int, int] = None) -> Image.Image:
        """读取基础目录下的图片资源，返回独立图片副本。

        本地文件更新后自动重新加载，指定 size 时缩放到对应尺寸。
        """
        fullpath = pjoin(self.dir, path)
        if not osp.exists(fullpath):
            raise FileNotFoundError(f"静态图片资源 {fullpath} 不存在")
        try:
            with self.lock:
                img, time = self.images.get(path, (None, None))
                mtime = int(os.path.getmtime(fullpath) * 1000)
                if mtime != time:
                    self.images[path] = (open_image(fullpath).convert('RGBA'), mtime)
                img, time = self.images[path]
            # 缓存始终保存原图；缩放在锁外生成结果，同尺寸或原尺寸也返回独立副本。
            if size:
                resized = resize_by_optional_size(img, size)
                return resized.copy() if resized is img else resized
            return img.copy()
        except:
            raise FileNotFoundError(f"读取静态图片资源 {fullpath} 失败")



# ================================ 网页Json资源 ================================ #

class WebJsonRes:
    def __init__(
        self,
        name: str,
        url: str | None = None,
        urls: List[str] | None = None,
        update_interval: timedelta = None,
        file_cache: bool = True,
    ):
        self.name = name
        self.url = url
        self.urls = self._normalize_urls(url, urls)
        self.update_interval = update_interval
        self.data: Any = None
        self.update_time: datetime = None
        self.hash: str = None
        self.last_success_url: str | None = None
        self.file_cache_path: str | None = None
        if file_cache:
            # 使用资源名作为文件名，方便在本地直接定位某个网页 JSON 的兜底缓存。
            self.file_cache_path = f"{SEKAI_DATA_DIR}/webjson/{self.name}.json"

    # ================================ 数据源归一化 ================================ #
    # 这里兼容旧版单地址调用和新版多地址回退调用，避免各业务方自己重复处理去空、去重逻辑。
    @staticmethod
    def _normalize_urls(url: str | None, urls: List[str] | None) -> List[str]:
        normalized: List[str] = []
        seen: set[str] = set()

        raw_urls: List[str] = []
        if urls is not None:
            raw_urls = [urls] if isinstance(urls, str) else list(urls)
        elif url is not None:
            raw_urls = [url]

        for item in raw_urls:
            item = (item or "").strip()
            if not item or item in seen:
                continue
            normalized.append(item)
            seen.add(item)
        return normalized
    
    async def _download(self, timeout: float):
        # ================================ 多地址顺序回退 ================================ #
        # 按配置顺序尝试网页 JSON 数据源；主地址故障时自动切换到后续备用地址。
        # 每个数据源独立使用调用方传入的超时时间，避免主源消耗备用源的可用时间。
        # 这里只负责“本次刷新该用哪个源”，旧数据续用逻辑仍由 _check_before_get 统一处理。
        if not self.urls:
            raise Exception(f"网页Json资源 [{self.name}] 未配置可用的数据源URL")

        errors: List[str] = []
        for idx, current_url in enumerate(self.urls, start=1):
            try:
                data = await asyncio.wait_for(
                    download_json(current_url),
                    timeout,
                )
                self.data = data
                self.hash = get_md5(dumps_json(self.data, indent=False).encode('utf-8'))
                self.update_time = datetime.now()
                self.last_success_url = current_url
                if self.file_cache_path:
                    try:
                        dump_json(self.data, self.file_cache_path, indent=False)
                    except Exception as cache_err:
                        logger.warning(
                            f"网页Json资源 [{self.name}] 写入本地缓存失败: {get_exc_desc(cache_err)}"
                        )
                logger.info(f"网页Json资源 [{self.name}] 更新成功，使用数据源: {current_url}")
                return
            except Exception as e:
                err = get_exc_desc(e)
                errors.append(f"[{idx}] {current_url} -> {truncate(err, 120)}")
                if idx < len(self.urls):
                    logger.warning(f"网页Json资源 [{self.name}] 数据源不可用，尝试下一个: {current_url} err={truncate(err, 120)}")

        raise Exception(f"网页Json资源 [{self.name}] 所有数据源更新失败: {' | '.join(errors)}")
    
    def _try_load_file_cache(self) -> bool:
        """
        当在线源全部失败且当前进程里没有旧数据时，尝试从磁盘缓存恢复。
        这里只做兜底，不替代正常的数据源选择流程。
        """
        if not self.file_cache_path or not os.path.exists(self.file_cache_path):
            return False
        try:
            self.data = load_json(self.file_cache_path)
            self.hash = get_md5(dumps_json(self.data, indent=False).encode('utf-8'))
            self.update_time = datetime.fromtimestamp(os.path.getmtime(self.file_cache_path))
            self.last_success_url = "[file-cache]"
            logger.warning(f"网页Json资源 [{self.name}] 从本地文件缓存中恢复")
            return True
        except Exception as cache_err:
            logger.warning(
                f"网页Json资源 [{self.name}] 读取本地缓存失败: {get_exc_desc(cache_err)}"
            )
            return False

    async def _check_before_get(self, timeout: float, raise_on_no_data: bool):
        if not self.data or not self.update_interval or datetime.now() - self.update_time > self.update_interval:
            try:
                await self._download(timeout)
            except Exception as e:
                if self.data:
                    logger.warning(f"更新网页Json资源 [{self.name}] 失败: {get_exc_desc(e)}，继续使用旧数据")
                else:
                    if self._try_load_file_cache():
                        return
                    if raise_on_no_data:
                        raise Exception(f"更新网页Json资源 [{self.name}] 失败: {get_exc_desc(e)}")
                    else:
                        logger.warning(f"更新网页Json资源 [{self.name}] 失败: {get_exc_desc(e)}，返回None")
                        self.data = None

    async def get(self, timeout: float = 5.0, raise_on_no_data: bool = True) -> Any | None:
        await self._check_before_get(timeout, raise_on_no_data)
        return self.data
    
    async def get_update_time(self, timeout: float = 5.0, raise_on_no_data: bool = True) -> datetime | None:
        await self._check_before_get(timeout, raise_on_no_data)
        return self.update_time
    
    async def get_hash(self, timeout: float = 5.0, raise_on_no_data: bool = True) -> str | None:
        await self._check_before_get(timeout, raise_on_no_data)
        return self.hash
    

