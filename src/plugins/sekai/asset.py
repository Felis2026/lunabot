from ..utils import *
from .common import *
import threading

asset_config = Config('sekai.asset')
ASSET_DEBUG_CFG = asset_config.item('debug')

# ================================ MasterData资源 ================================ #

DEFAULT_VERSION = "0.0.0.0"
MASTER_DB_CACHE_DIR = f"{SEKAI_ASSET_DIR}/masterdata/"
# compact 区服的 costume3ds 可能缺少 assetbundleName，
# 这里使用独立的 sidecar 补丁目录持久化补全结果，不改写上游原始 masterdata 文件。
MASTER_DB_PATCH_DIR = f"{SEKAI_ASSET_DIR}/masterdata_patch/"
DEFAULT_INDEX_KEYS = ['id']
DEFAULT_SORT_KEYS = []

# 运行期缓存：按区服缓存 costume3ds 资源名补丁，避免重复读取和重复生成。
_costume3ds_asset_patch_cache: Dict[str, Dict[int, str]] = {}
_costume3ds_asset_patch_meta: Dict[str, Tuple[str, str]] = {}
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

def build_costume3ds_assetbundle_patch(region: str) -> Dict[int, str]:
    # 为 compact 区服生成 costume3ds 的资源名补丁：
    # 仅对本区服缺少 assetbundleName 的条目，尝试用 JP 同 id 的资源名补齐。
    region_path = get_masterdata_cache_path(region, "costume3ds")
    jp_path = get_masterdata_cache_path("jp", "costume3ds")
    if not os.path.exists(region_path):
        logger.warning(f"MasterData [{region}.costume3ds] 补丁生成失败: 区服数据不存在")
        return {}
    if not os.path.exists(jp_path):
        logger.warning(f"MasterData [{region}.costume3ds] 补丁生成失败: JP 数据不存在")
        return {}

    region_costume3ds = load_json(region_path)
    jp_costume3ds = load_json(jp_path)

    jp_asset_map: Dict[int, str] = {}
    for item in jp_costume3ds:
        asset_name = item.get("assetbundleName") or item.get("_assetbundleName")
        if asset_name:
            jp_asset_map[item["id"]] = asset_name

    patch: Dict[int, str] = {}
    missing_count = 0
    for item in region_costume3ds:
        asset_name = item.get("assetbundleName") or item.get("_assetbundleName")
        if asset_name:
            continue
        missing_count += 1
        jp_asset_name = jp_asset_map.get(item["id"])
        if jp_asset_name:
            patch[item["id"]] = jp_asset_name

    logger.info(
        f"MasterData [{region}.costume3ds] 补丁生成完成: 缺失{missing_count}条, "
        f"JP补齐{len(patch)}条, 仍缺失{missing_count - len(patch)}条"
    )
    return patch

def load_costume3ds_assetbundle_patch(region: str) -> Dict[int, str]:
    # 按区服版本 + JP 版本加载补丁；版本未变化时直接复用缓存，
    # 版本变化时重新生成并写入 sidecar 文件。
    region_version = get_masterdata_cache_version(region, "costume3ds")
    jp_version = get_masterdata_cache_version("jp", "costume3ds")
    current_meta = (region_version, jp_version)

    with _costume3ds_asset_patch_lock:
        if _costume3ds_asset_patch_meta.get(region) == current_meta:
            return _costume3ds_asset_patch_cache.get(region, {})

        patch_path = get_masterdata_patch_path(region, "costume3ds_assetbundle_patch")
        patch: Dict[int, str] | None = None

        if os.path.exists(patch_path):
            try:
                patch_data = load_json(patch_path)
                if (
                    patch_data.get("region_version") == region_version
                    and patch_data.get("jp_version") == jp_version
                ):
                    patch = {
                        int(costume3d_id): asset_name
                        for costume3d_id, asset_name in patch_data.get("assetbundle_map", {}).items()
                        if asset_name
                    }
            except Exception as e:
                logger.warning(f"MasterData [{region}.costume3ds] 读取补丁缓存失败: {get_exc_desc(e)}")

        if patch is None:
            patch = build_costume3ds_assetbundle_patch(region)
            dump_json({
                "region_version": region_version,
                "jp_version": jp_version,
                "assetbundle_map": {str(costume3d_id): asset_name for costume3d_id, asset_name in patch.items()},
            }, patch_path, indent=False)

        _costume3ds_asset_patch_cache[region] = patch
        _costume3ds_asset_patch_meta[region] = current_meta
        return patch

def make_costume3ds_map_fn(region: str):
    def costume3ds_map_fn(costume3ds: List[Dict[str, Any]]):
        # 第一步：把 compact 数据中的 _assetbundleName 统一归一化到 assetbundleName。
        # 第二步：对仍然缺失资源名的条目，应用 sidecar 补丁进行补全。
        normalized_count = 0
        missing_count = 0
        for item in costume3ds:
            asset_name = item.get("assetbundleName") or item.get("_assetbundleName")
            if asset_name and item.get("assetbundleName") != asset_name:
                item["assetbundleName"] = asset_name
                normalized_count += 1
            if not item.get("assetbundleName"):
                missing_count += 1

        if missing_count == 0:
            return costume3ds

        patch = load_costume3ds_assetbundle_patch(region)
        patched_count = 0
        for item in costume3ds:
            if item.get("assetbundleName"):
                continue
            asset_name = patch.get(item["id"])
            if asset_name:
                item["assetbundleName"] = asset_name
                patched_count += 1

        remaining_count = sum(1 for item in costume3ds if not item.get("assetbundleName"))
        logger.info(
            f"MasterData [{region}.costume3ds] 映射完成: 本地归一化{normalized_count}条, "
            f"补丁补齐{patched_count}条, 剩余缺失{remaining_count}条"
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

    async def update(self):
        """
        更新所有MasterDB的版本信息
        """
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

    def _build_indexed_data(self, region: str):
        try:
            if self.data.get(region, None) is None:
                logger.warning(f"MasterData [{region}.{self.name}] 构建索引发生在数据加载前")
                return
            if not self.data[region] or not isinstance(self.data[region], list):
                return
            logger.debug(f"MasterData [{region}.{self.name}] 开始构建索引")
            self.indexed_data[region] = {}
            for key in self.index_keys.get(region, []):
                ind = {}
                for item in self.data[region]:
                    if key not in item: 
                        continue
                    k = item[key]
                    ind.setdefault(k, []).append(item)
                if ind:
                    self.indexed_data[region][key] = ind
            logger.debug(f"MasterData [{region}.{self.name}] 构建索引成功")
        except:
            logger.print_exc(f"MasterData [{region}.{self.name}] 构建索引失败")

    def _build_sorted_data(self, region: str):
        try:
            if self.data.get(region, None) is None:
                logger.warning(f"MasterData [{region}.{self.name}] 构建排序发生在数据加载前")
                return
            if not self.data[region] or not isinstance(self.data[region], list):
                return
            logger.debug(f"MasterData [{region}.{self.name}] 开始构建排序")
            self.sorted_data[region] = {}
            for key in self.sort_keys.get(region, []):
                sorted_list = sorted(self.data[region], key=lambda x: x.get(key))
                if sorted_list:
                    self.sorted_data[region][key] = sorted_list
            logger.debug(f"MasterData [{region}.{self.name}] 构建排序成功")
        except:
            logger.print_exc(f"MasterData [{region}.{self.name}] 构建排序失败")

    async def _load_from_cache(self, region: str):
        """
        从缓存加载数据
        """
        cache_path = self.get_cache_path(region)
        assert os.path.exists(cache_path), "缓存不存在"
        versions = file_db.get_copy("master_data_cache_versions", {}).get(region, {})
        assert self.name in versions, "缓存版本无效"
        self.version[region] = versions[self.name]
        self.data[region] = await aload_json(cache_path)
        logger.info(f"MasterData [{region}.{self.name}] 从本地加载成功")
        map_fn = self.map_fn.get('all', self.map_fn.get(region))
        if map_fn:
            self.data[region] = await run_in_pool(map_fn, self.data[region])
            logger.info(f"MasterData [{region}.{self.name}] 映射函数执行完成")
        await run_in_pool(self._build_indexed_data, region)
        await run_in_pool(self._build_sorted_data, region)

    async def _download_from_db(self, region: str, source: RegionMasterDbSource):
        """
        从远程数据源更新数据
        """
        cache_path = self.get_cache_path(region)
        if not source.base_url.endswith("/"):
            source.base_url += "/"
        url = f"{source.base_url}{self.name}.json"

        # 下载数据
        download_fn = self.download_fn.get('all', self.download_fn.get(region))
        timeout = asset_config.get('default_masterdata_download_timeout')
        async def _download():
            if not download_fn:
                self.data[region] = await download_json(url)
            else:
                self.data[region] = await download_fn(source.base_url)
        try:
            await asyncio.wait_for(_download(), timeout)
        except asyncio.TimeoutError:
            logger.warning(f"下载 MasterData [{region}.{self.name}] 超时")
            return
        self.version[region] = source.version

        # 缓存到本地
        versions = file_db.get("master_data_cache_versions", {})
        if region not in versions:
            versions[region] = {}
        versions[region][self.name] = self.version[region]
        file_db.set("master_data_cache_versions", versions)
        await adump_json(self.data[region], cache_path)
        logger.info(f"MasterData [{region}.{self.name}] 更新成功")

        # 执行映射函数
        map_fn = self.map_fn.get('all', self.map_fn.get(region))
        if map_fn:
            self.data[region] = await run_in_pool(map_fn, self.data[region])
            logger.info(f"MasterData [{region}.{self.name}] 映射函数执行完成")
        await run_in_pool(self._build_indexed_data, region)
        await run_in_pool(self._build_sorted_data, region)

        # 执行更新后回调
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

    async def _update_before_get(self, region: str):
        async with self.lock:
            # 从缓存加载
            if self.data.get(region) is None:
                try: 
                    await self._load_from_cache(region)
                except Exception as e:
                    logger.warning(f"MasterData [{region}.{self.name}] 从本地缓存加载失败: {e}")
            # 检查是否更新
            db_mgr = RegionMasterDbManager.get(region)
            source = await db_mgr.get_latest_source()
            if get_version_order(self.version.get(region, DEFAULT_VERSION)) < get_version_order(source.version):
                await self._download_from_db(region, source)
            # 检查是否存在，如果仍然不存在则报错
            if self.data.get(region) is None:
                raise Exception(f"获取 MasterData [{region}.{self.name}] 的数据失败")

    async def get_data(self, region: str):
        """
        获取数据
        """
        await self._update_before_get(region)
        return self.data[region]
    
    async def get_path(self, region: str):
        """
        获取数据文件路径
        """
        await self._update_before_get(region)
        return self.get_cache_path(region)
    
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
        cls.get(name).update_hooks.append((hook_name, regions, hook))

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
    resbox = await download_json(f"{base_url}/resourceBoxes.json")
    resbox_detail = await download_json(f"{base_url}/resourceBoxDetails.json")
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
                    raise Exception(f"请求失败: {resp.status}")
                return await resp.read()
        if not timeout:
            return await do_download()
        else:
            return await asyncio.wait_for(do_download(), timeout)

    async def get_asset(
        self,
        path: str, 
        use_cache=True, 
        allow_error=True, 
        default=None, 
        cache_expire_secs=None, 
        timeout: Union[int, ConfigItem]=DEFAULT_GET_RIP_ASSET_TIMEOUT_CFG,
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
            for cache_rel_path in self._iter_local_cache_paths(path):
                cache_path = pjoin(self.cache_dir, cache_rel_path)
                try:
                    assert os.path.exists(cache_path)
                    if cache_expire_secs is not None:
                        assert datetime.now().timestamp() - os.path.getmtime(cache_path) < cache_expire_secs
                    with open(cache_path, "rb") as f:
                        return f.read()
                except:
                    pass
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
                if use_cache:
                    create_parent_folder(cache_path)
                    with open(cache_path, "wb") as f:
                        f.write(data)
                return data
            
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
        cache_path = pjoin(self.cache_dir, path)
        # 首先尝试从缓存加载
        for cache_rel_path in self._iter_local_cache_paths(path):
            cache_path = pjoin(self.cache_dir, cache_rel_path)
            try:
                assert os.path.exists(cache_path)
                if cache_expire_secs is not None:
                    assert datetime.now().timestamp() - os.path.getmtime(cache_path) < cache_expire_secs
                return cache_path
            except:
                pass
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
        """
        # 尝试从图片缓存加载
        if use_img_cache and path in self.cached_images:
            return self.cached_images[path]
        data = await self.get_asset(path, use_cache, allow_error, default, cache_expire_secs, get_cfg_or_value(timeout))
        try: 
            img = open_image(io.BytesIO(data))
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
        data = await self.get_asset(path, use_cache, allow_error, default, cache_expire_secs, get_cfg_or_value(timeout))
        try: return loads_json(data)
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
        """
        基于基础目录获取指定路径的图片资源
        当图片在本地更新时，会自动重新加载
        指定size时会缩放图片到指定大小
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
                if size:
                    img = resize_by_optional_size(img, size)
                self.images[path] = (img, time) 
                return self.images[path][0]
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
    
    async def _download(self):
        # ================================ 多地址顺序回退 ================================ #
        # 按配置顺序尝试网页 JSON 数据源；主地址故障时自动切换到后续备用地址。
        # 这里只负责“本次刷新该用哪个源”，旧数据续用逻辑仍由 _check_before_get 统一处理。
        if not self.urls:
            raise Exception(f"网页Json资源 [{self.name}] 未配置可用的数据源URL")

        errors: List[str] = []
        for idx, current_url in enumerate(self.urls, start=1):
            try:
                data = await download_json(current_url)
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
                await asyncio.wait_for(self._download(), timeout)
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
    

