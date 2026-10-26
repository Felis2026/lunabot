"""衣装裁框运行时：延迟准备、共享会话、有限缓存与固定框回退。"""
import asyncio
from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import importlib.util
import json
import logging
import os
from pathlib import Path
import time
import urllib.request

try:
    from .costume_crop import direct_bounds, fixed_bounds, predict_direct_mask
except ImportError:
    from costume_crop import direct_bounds, fixed_bounds, predict_direct_mask

LOGGER = logging.getLogger(__name__)
MODEL_NAME = 'isnet-anime'
MODEL_MD5 = '6f184e756bb3bd901c8849220a83e38e'
MODEL_URL = 'https://github.com/danielgatis/rembg/releases/download/v0.0.0/isnet-anime.onnx'
RULE_VERSION = 3
CACHE_LIMIT = 1024
JOB_LIMIT = 32
DOWNLOAD_SECONDS = 180


@dataclass(frozen=True)
class CropResult:
    """记录最终裁框及来源；固定框不会写入模型结果缓存。"""
    bounds: tuple
    route: str


# ================================ 模型路径与完整性 ================================ #
def model_locations(data_dir):
    """沿用rembg目录约定；未安装rembg时也能复用其新旧目录。"""
    legacy = Path(os.path.expanduser(os.getenv('U2NET_HOME',
        os.path.join(os.getenv('XDG_DATA_HOME', '~'), '.u2net'))))
    home = legacy if os.getenv('U2NET_HOME') else Path(os.path.expanduser(os.getenv('REMBG_HOME',
        os.path.join(os.getenv('XDG_DATA_HOME'), 'rembg') if os.getenv('XDG_DATA_HOME') else '~/.rembg')))
    target = home / 'models' / MODEL_NAME / f'{MODEL_NAME}.onnx'
    if importlib.util.find_spec('rembg') is not None:
        from rembg.sessions.dis_anime import DisSession
        if hasattr(DisSession, 'model_dir'):
            target = Path(DisSession.model_dir()) / f'{MODEL_NAME}.onnx'
        else:
            target = Path(DisSession.u2net_home()) / f'{MODEL_NAME}.onnx'
    private = Path(data_dir) / 'models' / f'{MODEL_NAME}.onnx'
    candidates = list(dict.fromkeys((target, legacy / f'{MODEL_NAME}.onnx', private,
                                     private.with_name('isnetis.onnx'))))
    return candidates, target if importlib.util.find_spec('rembg') is not None else private


def verify_model(path):
    """同名模型不一定同权重；仅在加载或文件变化时验证一次。"""
    digest = hashlib.md5()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    if digest.hexdigest() != MODEL_MD5:
        raise ValueError('模型不是已确认的isnet-anime权重，或文件不完整')


def download_model(target):
    """单次有限下载；先核对完整性，再原子替换，失败保留原文件。"""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + f'.{os.getpid()}.part')
    started = time.monotonic()
    try:
        request = urllib.request.Request(MODEL_URL, headers={'User-Agent': 'Sekai-Costume-Model'})
        with urllib.request.urlopen(request, timeout=20) as response, temporary.open('wb') as stream:
            received = 0
            while chunk := response.read(256 * 1024):
                if time.monotonic() - started > DOWNLOAD_SECONDS:
                    raise TimeoutError('衣装模型下载超过180秒')
                received += len(chunk)
                if received > 256 * 1024 * 1024:
                    raise ValueError('衣装模型文件超出预期大小')
                stream.write(chunk)
        verify_model(temporary)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def create_session(path):
    """存在可用GPU提供器时优先使用，加载失败后允许CPU执行。"""
    import onnxruntime as runtime
    options = runtime.SessionOptions()
    options.intra_op_num_threads = 2
    options.inter_op_num_threads = 1
    available = runtime.get_available_providers()
    providers = [provider for provider in ('CUDAExecutionProvider', 'ROCMExecutionProvider',
                 'DmlExecutionProvider', 'CoreMLExecutionProvider') if provider in available][:1]
    providers.append('CPUExecutionProvider')
    try:
        session = runtime.InferenceSession(str(path), sess_options=options, providers=providers)
    except Exception:
        if len(providers) == 1:
            raise
        LOGGER.warning('衣装模型加速提供器初始化失败，改用CPU', exc_info=True)
        session = runtime.InferenceSession(str(path), sess_options=options, providers=['CPUExecutionProvider'])
    LOGGER.info('衣装模型加载完成：%s；执行器=%s', path, session.get_providers())
    if len(providers) > 1 and not any(provider in session.get_providers() for provider in providers[:-1]):
        LOGGER.warning('衣装模型未能启用%s，当前实际使用CPU；请检查GPU运行库', providers[0])
    return session


# ================================ 会话和后台准备 ================================ #
class CostumeRuntime:
    """一个事件循环内共享一份会话；下载、加载、推理和缓存I/O均在线程执行。"""
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.cache_path = self.data_dir / 'costume-crops.json'
        self.session = None
        self.model_id = None
        self.prepare_task = None
        self.attempted = False
        self.jobs = {}
        self.cache = OrderedDict()
        self.failures = OrderedDict()
        self.inference_lock = asyncio.Lock()
        self.notices = set()
        self.local_signature = None
        self.next_discovery = 0
        self.discovery_task = None
        self.stats = {'loads': 0, 'inferences': 0, 'cache_hits': 0, 'coalesced': 0}

    def notice(self, reason, message):
        """同类故障仅记录一次，避免群聊提示和每次查询刷日志。"""
        if reason not in self.notices:
            LOGGER.warning(message)
            self.notices.add(reason)

    def start_prepare(self):
        """首次衣装请求触发一次后台准备，当次仍走固定框。"""
        if not self.attempted:
            self.prepare_task = asyncio.create_task(self._prepare_async())
            self.attempted = True
        elif (self.prepare_task is None or self.prepare_task.done()) and time.monotonic() >= self.next_discovery:
            # 仅探测本地文件变化，不重复下载；允许外部准备入口或手工放入后自动恢复。
            self.next_discovery = time.monotonic() + 30
            if self.discovery_task is None or self.discovery_task.done():
                self.discovery_task = asyncio.create_task(self._discover())

    def _signature(self):
        """固定几个模型位置的stat，不扫描目录、不读取模型正文。"""
        candidates, target = model_locations(self.data_dir)
        versions = []
        for path in candidates:
            if path.is_file():
                stat = path.stat()
                versions.append((str(path), stat.st_size, stat.st_mtime_ns))
        return tuple(versions)

    async def _discover(self):
        """文件变化才重新加载；失败后原文件不变时继续固定回退。"""
        try:
            signature = await asyncio.to_thread(self._signature)
            if signature and signature != self.local_signature:
                await self.prepare(retry=True)
        except Exception:
            LOGGER.debug('衣装本地模型探测失败', exc_info=True)

    def _prepare(self):
        """在线程完成模型定位、一次校验和加载，不因查询失败无限重试。"""
        if importlib.util.find_spec('onnxruntime') is None:
            self.notice('runtime', '未安装 ONNX Runtime，已使用固定裁切。')
            return None
        candidates, target = model_locations(self.data_dir)
        self.local_signature = self._signature()
        path = None
        invalid = []
        for candidate in candidates:
            if candidate.is_file():
                try:
                    verify_model(candidate)
                    path = candidate
                    break
                except ValueError:
                    invalid.append(str(candidate))
        if path is None:
            if invalid:
                raise ValueError(f'现有模型文件校验失败：{invalid}')
            self.notice('missing', '衣装裁切模型未配置，已使用固定裁切；正在后台准备。')
            path = download_model(target)
        session = create_session(path)
        stat = path.stat()
        model_id = [MODEL_NAME, MODEL_MD5, stat.st_size, stat.st_mtime_ns, RULE_VERSION]
        cached = []
        try:
            payload = json.loads(self.cache_path.read_text(encoding='utf-8'))
            if payload.get('model') == model_id:
                entries = payload.get('entries', [])
                if isinstance(entries, list):
                    cached = [entry for entry in entries[-CACHE_LIMIT:]
                              if isinstance(entry, list) and len(entry) == 2
                              and isinstance(entry[0], str) and isinstance(entry[1], list)
                              and len(entry[1]) == 4]
        except (OSError, ValueError, TypeError):
            pass
        return session, model_id, cached

    async def prepare(self, retry=False):
        """准备入口允许人工重试；并发等待同一任务，不取消其他请求的加载。"""
        if self.session is not None:
            return True
        if self.prepare_task and not self.prepare_task.done():
            return await asyncio.shield(self.prepare_task)
        if self.attempted and not retry:
            return False
        self.attempted = True
        self.prepare_task = asyncio.create_task(self._prepare_async())
        return await asyncio.shield(self.prepare_task)

    async def _prepare_async(self):
        """后台任务独立持有加载结果，等待者取消也不能丢失已经加载的会话。"""
        try:
            prepared = await asyncio.to_thread(self._prepare)
            if prepared is None:
                return False
            self.session, self.model_id, entries = prepared
            for key, bounds in entries:
                if isinstance(key, str) and isinstance(bounds, list) and len(bounds) == 4:
                    self.cache[key] = tuple(bounds)
            self.stats['loads'] += 1
            self.notices.clear()
            return True
        except Exception as error:
            self.notice('load', f'衣装模型准备失败，已使用固定裁切；可通过准备入口重试。原因：{error}')
            LOGGER.debug('衣装模型准备失败详情', exc_info=True)
            return False

    # ================================ 裁框缓存与同键合并 ================================ #
    @staticmethod
    def valid_bounds(bounds, size):
        """验证缓存及推理框，不接受空框、越界或布尔值混入坐标。"""
        return (isinstance(bounds, (tuple, list)) and len(bounds) == 4
                and all(isinstance(value, int) and not isinstance(value, bool) for value in bounds)
                and 0 <= bounds[0] < bounds[2] <= size[0]
                and 0 <= bounds[1] < bounds[3] <= size[1])

    def _infer_and_save(self, image, key, layout='paired'):
        """串行推理降低内存峰值；缓存写盘失败不丢弃已经得到的有效框。"""
        bounds = tuple(direct_bounds(predict_direct_mask(self.session, image), image.size, layout))
        if not self.valid_bounds(bounds, image.size):
            raise ValueError('衣装裁框无效')
        self.stats['inferences'] += 1
        return bounds

    def _save(self, payload):
        """仅保存至有上限的小型索引；同进程写盘由推理锁串行化。"""
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.cache_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(payload, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
        os.replace(temporary, self.cache_path)

    async def _crop(self, image, key, layout='paired'):
        """同键仅有此任务执行；任一等待者取消不终止共享推理。"""
        try:
            async with self.inference_lock:
                bounds = await asyncio.to_thread(self._infer_and_save, image, key, layout)
                self.cache[key] = bounds
                self.cache.move_to_end(key)
                while len(self.cache) > CACHE_LIMIT:
                    self.cache.popitem(last=False)
                payload = {'model': self.model_id, 'entries': list(self.cache.items())}
                try:
                    await asyncio.to_thread(self._save, payload)
                except OSError:
                    self.notice('cache', '衣装裁框缓存写入失败，本次仍使用模型结果。')
                self.notices.discard('inference')
                return CropResult(bounds, 'model')
        except Exception as error:
            self.notice('inference', f'衣装模型裁切失败，已使用固定裁切。原因：{error}')
            LOGGER.debug('衣装模型推理失败详情', exc_info=True)
            self.failures[key] = time.monotonic() + 60
            while len(self.failures) > CACHE_LIMIT:
                self.failures.popitem(last=False)
            return CropResult(tuple(fixed_bounds(image.size, layout)), 'fixed_inference_error')
        finally:
            self.jobs.pop(key, None)
            image.close()

    async def bounds(self, image, resource_version, *, layout='paired'):
        """按素材构图查缓存或异步推理，未就绪时立即使用同构图固定框。"""
        if layout not in ('paired', 'wide'):
            raise ValueError('不支持的衣装构图')
        fallback = tuple(fixed_bounds(image.size, layout))
        if self.session is None:
            self.start_prepare()
            return CropResult(fallback, 'fixed_preparing')
        key = json.dumps([resource_version, image.size, self.model_id], ensure_ascii=False, separators=(',', ':'))
        if layout != 'paired':
            # 横向多人图使用独立规则键，日服双图继续复用既有缓存。
            key = json.dumps([resource_version, image.size, self.model_id, layout, 2], ensure_ascii=False, separators=(',', ':'))
        if key in self.cache:
            bounds = self.cache.pop(key)
            if self.valid_bounds(bounds, image.size):
                self.cache[key] = bounds
                self.stats['cache_hits'] += 1
                return CropResult(bounds, 'model_cache')
        if self.failures.get(key, 0) > time.monotonic():
            return CropResult(fallback, 'fixed_recent_error')
        if key in self.jobs:
            self.stats['coalesced'] += 1
            return await asyncio.shield(self.jobs[key])
        if len(self.jobs) >= JOB_LIMIT:
            return CropResult(fallback, 'fixed_busy')
        task = asyncio.create_task(self._crop(image.copy(), key, layout))
        self.jobs[key] = task
        return await asyncio.shield(task)


_RUNTIMES = {}


def get_runtime(data_dir):
    """按持久化目录复用同一管理器，调用方不需要增加配置开关。"""
    key = str(Path(data_dir).resolve())
    if key not in _RUNTIMES:
        _RUNTIMES[key] = CostumeRuntime(data_dir)
    return _RUNTIMES[key]


if __name__ == '__main__':
    import sys
    directory = Path.cwd() / 'data/sekai/gacha'
    manager = get_runtime(directory)
    success = asyncio.run(manager.prepare(retry=True))
    print(f'衣装模型准备{"成功" if success else "失败"}；数据目录：{directory}')
    sys.exit(0 if success else 1)
