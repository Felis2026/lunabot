from ..utils import *
from .mirage import generate_mirage
from PIL import Image, ImageOps, ImageEnhance
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from functools import lru_cache
from enum import Enum
from pathlib import Path
import asyncio
import math
import os
import struct
import subprocess
import numpy as np


config = Config('imgtool')
logger = get_logger("ImgTool")
file_db = get_file_db("data/imgtool/db.json", logger)
cd = ColdDown(file_db, logger)
gbl = get_group_black_list(file_db, logger, 'imgtool', allow_group_admin_current_group=True)


class ImageOperationUnavailableError(RuntimeError):
    """表示图片操作依赖的可选能力未启用，应直接向用户返回提示。"""


# ============================= cpp程序调用 ============================= # 

@dataclass
class CppImageOutput:
    image: Image.Image | List[Image.Image]
    extra_info: dict


CPP_OUTPUT_MAX_BYTES = 1_000_000_000
CPP_EXTRA_INFO_MAX_BYTES = 1024 * 1024


def _read_exact(file_obj, size: int, field_name: str) -> bytes:
    """读取固定长度的二进制字段，拒绝被截断的 C++ 输出。"""
    data = file_obj.read(size)
    if len(data) != size:
        raise RuntimeError(f"imgtool-cpp返回的{field_name}数据不完整")
    return data


def execute_imgtool_cpp(image: Image.Image | List[Image.Image], command: str, *args) -> CppImageOutput:
    """通过无 Shell 的二进制协议调用 imgtool-cpp，并严格校验返回数据。"""
    is_single_frame = isinstance(image, Image.Image)
    if is_single_frame:
        image = [image]
    if not image:
        raise ValueError("imgtool-cpp输入图片不能为空")

    input_width, input_height = image[0].size
    input_frame_count = len(image)
    if input_width <= 0 or input_height <= 0:
        raise ValueError("imgtool-cpp输入图片尺寸无效")
    if any(frame.size != (input_width, input_height) for frame in image):
        raise ValueError("imgtool-cpp要求所有输入帧尺寸一致")

    ret: List[Image.Image] = []
    with TempFilePath('input') as input_path:
        with TempFilePath('output') as output_path:
            # 保存输入文件
            with open(input_path, 'wb') as f:
                f.write(struct.pack('<iii', input_frame_count, input_height, input_width))
                for frame in image:
                    frame = frame.convert('RGBA')
                    f.write(frame.tobytes('raw', 'RGBA'))

            cli_path = Path(config.get('cpp_binary_path', 'data/imgtool/imgtool-cpp'))
            timeout_seconds = int(config.get('cpp_timeout_seconds', 30))
            logger.info(
                f"调用imgtool-cpp程序: {command} {' '.join(map(str, args))} "
                f"输入尺寸: {input_frame_count}x{input_width}x{input_height}"
            )
            if not cli_path.is_file():
                raise RuntimeError("imgtool-cpp程序不存在，请使用src/scripts/compile_imgtool_cpp.sh编译")

            try:
                result = subprocess.run(
                    [str(cli_path), input_path, output_path, command, *map(str, args)],
                    capture_output=True,
                    text=True,
                    timeout=timeout_seconds,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(f"imgtool-cpp执行超过{timeout_seconds}秒，已终止") from exc

            if result.returncode != 0:
                error_text = (result.stderr or result.stdout or "未知错误").strip()
                raise RuntimeError(f"imgtool-cpp执行失败({result.returncode}): {error_text[:500]}")
            if not os.path.isfile(output_path):
                raise RuntimeError("imgtool-cpp未生成输出文件")

            # 读取输出文件
            with open(output_path, 'rb') as f:
                frame_count, height, width = struct.unpack('<iii', _read_exact(f, 12, "头部"))
                if frame_count <= 0 or height <= 0 or width <= 0:
                    raise RuntimeError("imgtool-cpp返回了无效的图片尺寸")
                if frame_count != input_frame_count:
                    raise RuntimeError("imgtool-cpp返回的帧数与输入不一致")
                pixel_bytes = frame_count * height * width * 4
                if pixel_bytes > CPP_OUTPUT_MAX_BYTES:
                    raise RuntimeError("imgtool-cpp返回的图片数据过大")

                for _ in range(frame_count):
                    frame_data = _read_exact(f, width * height * 4, "像素")
                    frame = Image.frombytes('RGBA', (width, height), frame_data, 'raw', 'RGBA')
                    ret.append(frame)

                extra_info_length = struct.unpack('<i', _read_exact(f, 4, "附加信息长度"))[0]
                if not 0 <= extra_info_length <= CPP_EXTRA_INFO_MAX_BYTES:
                    raise RuntimeError("imgtool-cpp返回的附加信息长度无效")
                extra_info = loads_json(_read_exact(f, extra_info_length, "附加信息")) if extra_info_length else {}
                if not isinstance(extra_info, dict):
                    raise RuntimeError("imgtool-cpp返回的附加信息格式无效")
                if f.read(1):
                    raise RuntimeError("imgtool-cpp输出包含未识别的尾部数据")

            logger.info(
                f"imgtool-cpp程序执行完毕，输出尺寸: {frame_count}x{width}x{height}，"
                f"额外返回: {extra_info}"
            )

    return CppImageOutput(
        image=ret[0] if is_single_frame else ret, 
        extra_info=extra_info,
    )

def cutout_image(image: Image.Image | List[Image.Image], tolerance: int) -> CppImageOutput:
    """
    抠图
    - tolerance: rgb距离平方的容差
    """
    return execute_imgtool_cpp(image, "cutout", tolerance)

def shrink_image(image: Image.Image | List[Image.Image], alpha_threshold: int, edge: int) -> CppImageOutput:
    """
    将图片边缘的透明部分裁剪掉，
    - alpha_threshold: alpha通道阈值，
    - edge: 裁剪后保留的边缘宽度
    - extra_ret: 返回扣完图的部分在原图中的bbox { 'bbox': (x, y, w, h) }
    """
    try:
        return execute_imgtool_cpp(image, "shrink", alpha_threshold, edge)
    except Exception as e:
        # C++ 能力是性能优化，不应成为裁剪透明功能的单点故障。
        logger.warning(f"imgtool-cpp程序shrink命令执行失败，使用备用实现: {get_exc_desc(e)}")
        is_single_frame = isinstance(image, Image.Image)
        frames = [image] if is_single_frame else list(image)
        if not frames:
            raise ValueError("裁剪透明输入不能为空")

        width, height = frames[0].size
        if any(frame.size != (width, height) for frame in frames):
            raise ValueError("裁剪透明要求所有输入帧尺寸一致")

        opaque_mask = np.zeros((height, width), dtype=bool)
        rgba_frames = []
        for frame in frames:
            rgba_frame = frame.convert('RGBA')
            rgba_frames.append(rgba_frame)
            opaque_mask |= np.asarray(rgba_frame.getchannel('A')) > alpha_threshold

        positions = np.argwhere(opaque_mask)
        if positions.size == 0:
            output_frames = [frame.copy() for frame in rgba_frames]
            bbox = [0, 0, width, height]
        else:
            top, left = positions.min(axis=0)
            bottom, right = positions.max(axis=0) + 1
            output_left = int(left) - edge
            output_top = int(top) - edge
            output_width = int(right - left) + 2 * edge
            output_height = int(bottom - top) + 2 * edge
            source_box = (
                max(0, output_left),
                max(0, output_top),
                min(width, output_left + output_width),
                min(height, output_top + output_height),
            )
            paste_position = (max(0, -output_left), max(0, -output_top))
            output_frames = []
            for frame in rgba_frames:
                output = Image.new('RGBA', (output_width, output_height), (0, 0, 0, 0))
                output.paste(frame.crop(source_box), paste_position)
                output_frames.append(output)
            bbox = [output_left, output_top, output_width, output_height]

        return CppImageOutput(
            image=output_frames[0] if is_single_frame else output_frames,
            extra_info={'bbox': bbox},
        )


# ============================= 基础设施 ============================= # 

IMAGE_LIST_CLEAN_INTERVAL_CFG = config.item('image_list_clean_interval')  # 图片列表清理间隔(s)
MULTI_IMAGE_MAX_NUM_CFG = config.item('multi_image_max_num')  # 多张图片操作的最大数量
IMGTOOL_MAX_CONCURRENT_JOBS = max(1, int(config.get('max_concurrent_jobs', 2)))
IMGTOOL_MAX_QUEUED_JOBS = max(0, int(config.get('max_queued_jobs', 4)))
STATIC_OUTPUT_PIXEL_LIMIT = parse_cfg_num(config.get('output_limits.static_pixels', '1024*1024*16'))
ANIMATED_OUTPUT_PIXEL_LIMIT = parse_cfg_num(config.get('output_limits.animated_pixels', '1024*1024*32'))
ANIMATED_OUTPUT_MAX_FRAMES = max(1, int(config.get('output_limits.max_frames', 100)))


# ================================ 并发与资源预算 ================================ #
# ImgTool 的 Pillow、NumPy 和外部程序任务都可能瞬时占用较多内存。独立线程池避免
# 挤占 Bot 公共线程池；显式排队上限则避免 ThreadPoolExecutor 的无界队列持续堆积。
IMGTOOL_EXECUTOR = ThreadPoolExecutor(
    max_workers=IMGTOOL_MAX_CONCURRENT_JOBS,
    thread_name_prefix="imgtool",
)


class ImgToolJobGate:
    """限制同时执行和等待的 ImgTool 重型任务数量。"""

    def __init__(self, max_concurrent: int, max_queued: int):
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._state_lock = asyncio.Lock()
        self._accepted_jobs = 0
        self._capacity = max_concurrent + max_queued

    async def acquire(self) -> None:
        async with self._state_lock:
            if self._accepted_jobs >= self._capacity:
                raise ReplyException(
                    f"当前图片处理任务较多（最多同时处理{IMGTOOL_MAX_CONCURRENT_JOBS}个、"
                    f"排队{IMGTOOL_MAX_QUEUED_JOBS}个），请稍后再试"
                )
            self._accepted_jobs += 1

        try:
            await self._semaphore.acquire()
        except BaseException:
            async with self._state_lock:
                self._accepted_jobs -= 1
            raise

    async def release(self) -> None:
        self._semaphore.release()
        async with self._state_lock:
            self._accepted_jobs -= 1


IMGTOOL_JOB_GATE = ImgToolJobGate(
    IMGTOOL_MAX_CONCURRENT_JOBS,
    IMGTOOL_MAX_QUEUED_JOBS,
)


@asynccontextmanager
async def imgtool_job_slot():
    """为一次完整图片任务申请槽位，取消或异常时也会正确释放。"""
    await IMGTOOL_JOB_GATE.acquire()
    try:
        yield
    finally:
        await IMGTOOL_JOB_GATE.release()


@on_shutdown()
def _shutdown_imgtool_executor():
    IMGTOOL_EXECUTOR.shutdown(wait=False, cancel_futures=True)


def _resize_images_to_total_limit(
    images: List[Image.Image],
    pixel_limit: int,
) -> List[Image.Image]:
    """保留全部独立图片；必要时缩放尺寸并在各自动图内部抽帧。"""
    total_pixels = get_image_pixels(images)
    if total_pixels <= pixel_limit:
        return images
    if not images or pixel_limit < len(images):
        raise ReplyException("图片数量或尺寸过大，无法在安全范围内处理")

    try:
        resized = limit_image_by_pixels(images, pixel_limit, allow_frame_drop=False)
    except ValueError as exc:
        raise ReplyException("图片尺寸过大，无法在安全范围内缩放") from exc
    if get_image_pixels(resized) > pixel_limit:
        raise ReplyException("图片尺寸过大，无法在安全范围内缩放")
    return resized


def _fit_uniform_size(width: int, height: int, units: int, pixel_limit: int) -> tuple[int, int]:
    """计算多帧或多图共同使用的安全尺寸，不分配目标画布。"""
    required_pixels = width * height * units
    if required_pixels <= pixel_limit:
        return width, height
    if units <= 0 or pixel_limit < units:
        raise ReplyException("预计输出图片过大，无法安全处理")

    scale = math.sqrt(pixel_limit / required_pixels)
    safe_width = max(1, int(width * scale))
    safe_height = max(1, int(height * scale))
    while safe_width * safe_height * units > pixel_limit:
        if safe_width >= safe_height and safe_width > 1:
            safe_width -= 1
        elif safe_height > 1:
            safe_height -= 1
        else:
            raise ReplyException("预计输出图片过大，无法安全处理")
    return safe_width, safe_height


def _limit_animated_image_by_budget(
    image: Image.Image,
    pixel_limit: int,
    frame_limit: int,
) -> tuple[Image.Image, bool, tuple[int, int], tuple[int, int]]:
    """按时间轴限制动图；返回结果、是否调整及调整前后的“帧数/总像素”。"""
    original_stats = (
        image.n_frames,
        image.width * image.height * image.n_frames,
    )
    if original_stats[0] <= frame_limit and original_stats[1] <= pixel_limit:
        return image, False, original_stats, original_stats

    frames, durations = get_gif_timeline(image)
    try:
        frames, durations = limit_gif_timeline_by_pixels(
            frames,
            durations,
            pixel_limit,
            max_frames=frame_limit,
            allow_frame_drop=True,
        )
    except ValueError as exc:
        raise ReplyException("动图尺寸过大，无法在安全范围内缩放") from exc
    output_stats = (len(frames), get_image_pixels(frames))
    result = frames_to_gif(
        frames,
        quantize_gif_durations(durations),
    )
    return result, True, original_stats, output_stats


def _enforce_output_budget(image: Image.Image | List[Image.Image], operation_name: str):
    """在每步操作后统一校验结果，避免后续操作继承超限对象。"""
    if isinstance(image, list):
        original_pixels = get_image_pixels(image)
        limited = _resize_images_to_total_limit(image, STATIC_OUTPUT_PIXEL_LIMIT)
        if get_image_pixels(limited) != original_pixels:
            logger.info(
                f"图片操作 {operation_name} 输出多图超限，已缩放 "
                f"{original_pixels} -> {get_image_pixels(limited)} 像素"
            )
        return limited

    if is_animated(image):
        limited, adjusted, original_stats, output_stats = _limit_animated_image_by_budget(
            image,
            ANIMATED_OUTPUT_PIXEL_LIMIT,
            ANIMATED_OUTPUT_MAX_FRAMES,
        )
        if adjusted:
            logger.info(
                f"图片操作 {operation_name} 动图输出超限，已调整 "
                f"{original_stats[0]}帧/{original_stats[1]}像素 -> "
                f"{output_stats[0]}帧/{output_stats[1]}像素"
            )
        return limited

    original_size = image.size
    limited = limit_image_by_pixels(image, STATIC_OUTPUT_PIXEL_LIMIT)
    if limited.size != original_size:
        logger.info(f"图片操作 {operation_name} 静态输出超限，已缩放 {original_size} -> {limited.size}")
    return limited


# 图片类型
class ImageType(Enum):
    Any         = 1
    Animated    = 2
    Static      = 3
    Multiple    = 4

    def __str__(self):
        if self == ImageType.Any:
            return "任意单图"
        elif self == ImageType.Animated:
            return "动图"
        elif self == ImageType.Static:
            return "静态图"
        elif self == ImageType.Multiple:
            return "多张图片"

    def check_img(self, img) -> bool:
        if self == ImageType.Multiple:
            if not isinstance(img, list) or not img:
                return False
            return all(isinstance(item, Image.Image) and not is_animated(item) for item in img)
        elif self == ImageType.Any:
            return isinstance(img, Image.Image)
        elif self == ImageType.Animated:
            return isinstance(img, Image.Image) and is_animated(img)
        elif self == ImageType.Static:
            return isinstance(img, Image.Image) and not is_animated(img)
        return False

    def check_type(self, tar) -> bool:
        if self == ImageType.Multiple or tar == ImageType.Multiple:
            return self == tar
        # Any 仅代表单张图片；静态/动图的最终匹配在每一步运行时再次确认。
        return self == tar or self == ImageType.Any or tar == ImageType.Any

    @classmethod
    def get_type(cls, img) -> 'ImageType':
        if isinstance(img, list):
            return ImageType.Multiple
        elif is_animated(img):
            return ImageType.Animated
        else:
            return ImageType.Static
        
# 图片操作基类
class ImageOperation:
    all_ops = {}

    def __init__(self, name: str, input_type: ImageType, output_type: ImageType, process_type: str='batch'):
        self.name = name
        self.input_type = input_type
        self.output_type = output_type
        self.process_type = process_type
        self.help = ""
        self.input_limit = parse_cfg_num(config.get('input_res_limit.default'))
        ImageOperation.all_ops[name] = self
        assert_and_reply(process_type in ['single', 'batch'], f"图片操作类型{process_type}错误")
        assert_and_reply(not (input_type == ImageType.Multiple and process_type == 'batch'), f"多张图片操作不能以批量方式处理")

    def parse_args(self, args: List[str]) -> dict:
        return None

    def operate(self, img: Image.Image, args: dict=None, image_type: ImageType=None, frame_idx: int=0, total_frame: int=1) -> Image.Image:
        raise NotImplementedError()

    def __call__(self, img: Image.Image, args: List[str]) -> Image.Image:
        try:
            args = self.parse_args(args)
        except Exception as e:
            if str(e):
                msg = f"参数错误: {e}\n{self.help}"
            else:
                msg = f"参数错误\n{self.help}"
            raise ReplyException(msg.strip())
        operation_input_limit = (
            args.get('_input_limit', self.input_limit)
            if isinstance(args, dict)
            else self.input_limit
        )
        
        input_type = ImageType.get_type(img)
        is_batched_multiple = input_type == ImageType.Multiple and self.input_type != ImageType.Multiple
        if is_batched_multiple:
            assert_and_reply(
                all(self.input_type.check_img(item) for item in img),
                f"操作 {self.name} 需要 {self.input_type} 输入",
            )
        else:
            assert_and_reply(
                self.input_type.check_img(img),
                f"操作 {self.name} 需要 {self.input_type} 输入，实际为 {input_type}",
            )

        def apply_limit(img: Union[Image.Image, List[Image.Image]]):
            if isinstance(img, Image.Image) and not is_animated(img):
                w, h = img.size
                img = limit_image_by_pixels(img, operation_input_limit)
                new_w, new_h = img.size
                if (w, h) != (new_w, new_h):
                    logger.info(f"图片操作 {self.name} 对超限输入进行缩放 {w}x{h} -> {new_w}x{new_h}")
            elif isinstance(img, Image.Image):
                img, adjusted, original_stats, output_stats = _limit_animated_image_by_budget(
                    img,
                    operation_input_limit,
                    ANIMATED_OUTPUT_MAX_FRAMES,
                )
                if adjusted:
                    logger.info(
                        f"图片操作 {self.name} 对超限动图输入进行缩放/抽帧 "
                        f"{original_stats[0]}帧/{original_stats[1]}像素 -> "
                        f"{output_stats[0]}帧/{output_stats[1]}像素"
                    )
            else:
                original_pixels = get_image_pixels(img)
                img = limit_image_by_pixels(img, operation_input_limit, allow_frame_drop=False)
                if get_image_pixels(img) != original_pixels:
                    logger.info(
                        f"图片操作 {self.name} 对超限多图输入进行缩放 "
                        f"{original_pixels} -> {get_image_pixels(img)} 像素"
                    )
            return img

        def process_image(img):
            img_type = ImageType.get_type(img)
            if self.process_type == 'single':
                return self.operate(apply_limit(img), args, img_type)
            elif self.process_type == 'batch':
                if img_type == ImageType.Animated:
                    limited_img = apply_limit(img)
                    frames, durations = get_gif_timeline(limited_img)
                    frames = [self.operate(frame, args, img_type, i, len(frames)) for i, frame in enumerate(frames)]
                    return frames_to_gif(
                        frames,
                        quantize_gif_durations(durations),
                    )
                else:
                    return self.operate(apply_limit(img), args, img_type)

        def output_matches(item) -> bool:
            if self.output_type.check_img(item):
                return True
            # Pillow 会把视觉上完全相同的 GIF 帧合并成单帧；这种退化结果仍是有效输出，
            # 但后续若接倒放等动图专用操作，下一步的运行时输入校验仍会拒绝它。
            return (
                self.output_type == ImageType.Animated
                and isinstance(item, Image.Image)
                and item.format == 'GIF'
            )
        
        log_args = (
            {key: value for key, value in args.items() if not key.startswith('_')}
            if isinstance(args, dict)
            else args
        )
        logger.info(f"执行图片操作:{self.name} 输入类型:{input_type} 参数:{log_args}")
        if is_batched_multiple:
            logger.info(f"为 {self.name} 操作批量处理 {len(img)} 张图片")
            processed = [process_image(item) for item in img]
            if self.output_type == ImageType.Multiple:
                result = [nested_item for item in processed for nested_item in item]
                assert_and_reply(self.output_type.check_img(result), f"操作 {self.name} 返回了错误的图片类型")
            else:
                result = processed
                assert_and_reply(
                    all(output_matches(item) for item in result),
                    f"操作 {self.name} 返回了错误的图片类型",
                )
        else:
            result = process_image(img)
            assert_and_reply(output_matches(result), f"操作 {self.name} 返回了错误的图片类型")

        return _enforce_output_budget(result, self.name)
            
                
# 从回复消息获取第一张图片
async def get_reply_fst_image(ctx: HandlerContext, return_url=False):
    img_url = await ctx.aget_image_urls(return_first=True)
    if return_url: return img_url
    try:
        img = await download_image(img_url)
    except Exception as e:
        logger.print_exc(f"获取图片 {img_url} 失败")
        await ctx.asend_reply_msg("获取图片失败")
        raise NoReplyException()
    return img

# ================================ 图片列表隔离 ================================ #
# 图片列表按“群 + 用户”隔离；私聊使用独立作用域。锁覆盖整个读改写过程，避免同一
# 用户快速连续 push/pop 时互相覆盖。旧版按用户存储的数据会在首次访问时就地迁移。
IMAGE_LIST_LOCK = asyncio.Lock()


def _get_image_list_scope(ctx: HandlerContext) -> tuple[str, str]:
    user_id = str(ctx.user_id)
    group_id = getattr(ctx, 'group_id', None)
    if group_id:
        return f"group:{group_id}:user:{user_id}", user_id
    return f"private:user:{user_id}", user_id


def _load_image_list_state(scope_key: str, legacy_user_key: str):
    """加载、清理并迁移图片列表状态；调用方必须持有 IMAGE_LIST_LOCK。"""
    image_lists = file_db.get('image_list', {})
    edit_times = file_db.get('image_list_edit_time', {})
    if not isinstance(image_lists, dict):
        image_lists = {}
    if not isinstance(edit_times, dict):
        edit_times = {}

    now = datetime.now().timestamp()
    expire_seconds = IMAGE_LIST_CLEAN_INTERVAL_CFG.get()

    if scope_key not in image_lists and legacy_user_key in image_lists:
        image_lists[scope_key] = image_lists.pop(legacy_user_key)
        edit_times[scope_key] = edit_times.pop(legacy_user_key, now)
        logger.info(f"已将用户 {legacy_user_key} 的旧版图片列表迁移到 {scope_key}")

    expired_keys = []
    for key, timestamp in list(edit_times.items()):
        try:
            expired = now - float(timestamp) > expire_seconds
        except (TypeError, ValueError):
            expired = True
        if expired:
            expired_keys.append(key)

    for key in expired_keys:
        image_lists.pop(key, None)
        edit_times.pop(key, None)
        logger.info(f"图片列表 {key} 已过期并清理")

    # 清理单边写入遗留的孤儿键，保证两份状态始终一一对应。
    for key in list(image_lists):
        if key not in edit_times and key != scope_key:
            image_lists.pop(key, None)
    for key in list(edit_times):
        if key not in image_lists and key != scope_key:
            edit_times.pop(key, None)

    image_lists.setdefault(scope_key, [])
    if not isinstance(image_lists[scope_key], list):
        image_lists[scope_key] = []
    edit_times[scope_key] = now
    return image_lists, edit_times


async def get_image_list(ctx: HandlerContext) -> List[str]:
    """返回当前会话作用域的图片 URL 快照。"""
    scope_key, legacy_user_key = _get_image_list_scope(ctx)
    async with IMAGE_LIST_LOCK:
        image_lists, edit_times = _load_image_list_state(scope_key, legacy_user_key)
        file_db.set('image_list', image_lists)
        file_db.set('image_list_edit_time', edit_times)
        snapshot = list(image_lists[scope_key])
    logger.info(f"获取图片列表 {scope_key}，共有 {len(snapshot)} 张图片")
    return snapshot

# 往图片列表push图片
async def add_image_to_list(ctx: HandlerContext, reply=True):
    args = ctx.get_args().strip().split()
    assert_and_reply(not args or args == ['r'], "仅支持参数 r（倒序添加）")
    img_urls = await ctx.aget_image_urls(min_count=1, max_count=None)
    if args == ['r']:
        img_urls = img_urls[::-1]

    scope_key, legacy_user_key = _get_image_list_scope(ctx)
    async with IMAGE_LIST_LOCK:
        image_lists, edit_times = _load_image_list_state(scope_key, legacy_user_key)
        current_list = image_lists[scope_key]
        max_num = MULTI_IMAGE_MAX_NUM_CFG.get()
        assert_and_reply(
            len(current_list) + len(img_urls) <= max_num,
            f"图片列表已满，当前有{len(current_list)}张图片，最多只能处理{max_num}张图片",
        )
        current_list.extend(img_urls)
        file_db.set('image_list', image_lists)
        file_db.set('image_list_edit_time', edit_times)
        current_count = len(current_list)

    logger.info(f"图片列表 {scope_key} 添加了 {len(img_urls)} 张图片，共有 {current_count} 张")
    if reply:
        return await ctx.asend_reply_msg(f"成功添加{len(img_urls)}张图片，当前有{current_count}张图片")

# 从图片列表pop图片
async def pop_image_from_list(ctx: HandlerContext, reply=True):
    scope_key, legacy_user_key = _get_image_list_scope(ctx)
    async with IMAGE_LIST_LOCK:
        image_lists, edit_times = _load_image_list_state(scope_key, legacy_user_key)
        assert_and_reply(image_lists[scope_key], "图片列表为空")
        img = image_lists[scope_key].pop()
        remaining_count = len(image_lists[scope_key])
        file_db.set('image_list', image_lists)
        file_db.set('image_list_edit_time', edit_times)
    logger.info(f"图片列表 {scope_key} 移除一张图片，剩余 {remaining_count} 张")
    img = await get_image_cq(img)
    if reply:
        return await ctx.asend_reply_msg(f"{img}移除该图片，剩余{remaining_count}张图片")

# 清空图片列表
async def clear_image_list(ctx: HandlerContext, reply=True):
    scope_key, legacy_user_key = _get_image_list_scope(ctx)
    async with IMAGE_LIST_LOCK:
        image_lists, edit_times = _load_image_list_state(scope_key, legacy_user_key)
        previous_count = len(image_lists[scope_key])
        image_lists[scope_key].clear()
        file_db.set('image_list', image_lists)
        file_db.set('image_list_edit_time', edit_times)
    logger.info(f"图片列表 {scope_key} 已清空，之前有 {previous_count} 张图片")
    if reply:
        return await ctx.asend_reply_msg(f"清空列表中 {previous_count} 张图片")


async def consume_image_list_snapshot(ctx: HandlerContext, consumed_urls: List[str]) -> None:
    """仅移除本次操作读取的列表前缀，保留处理期间新追加的图片。"""
    if not consumed_urls:
        return
    scope_key, legacy_user_key = _get_image_list_scope(ctx)
    async with IMAGE_LIST_LOCK:
        image_lists, edit_times = _load_image_list_state(scope_key, legacy_user_key)
        current_list = image_lists[scope_key]
        if current_list[:len(consumed_urls)] != consumed_urls:
            logger.warning(f"图片列表 {scope_key} 在处理期间发生变更，本次不自动消费列表")
            file_db.set('image_list', image_lists)
            file_db.set('image_list_edit_time', edit_times)
            return
        del current_list[:len(consumed_urls)]
        file_db.set('image_list', image_lists)
        file_db.set('image_list_edit_time', edit_times)
    logger.info(f"图片列表 {scope_key} 已消费本次使用的 {len(consumed_urls)} 张图片")


# 翻转图片列表
async def reverse_image_list(ctx: HandlerContext, reply=True):
    scope_key, legacy_user_key = _get_image_list_scope(ctx)
    async with IMAGE_LIST_LOCK:
        image_lists, edit_times = _load_image_list_state(scope_key, legacy_user_key)
        image_lists[scope_key].reverse()
        current_count = len(image_lists[scope_key])
        file_db.set('image_list', image_lists)
        file_db.set('image_list_edit_time', edit_times)
    logger.info(f"图片列表 {scope_key} 已翻转")
    if reply:
        return await ctx.asend_reply_msg(f"翻转成功，当前列表有{current_count}张图片")

# 获取多张图片
async def get_multi_images(ctx: HandlerContext) -> tuple[Image.Image | List[Image.Image], bool, List[str]]:
    max_num = MULTI_IMAGE_MAX_NUM_CFG.get()
    img_urls = await ctx.aget_image_urls(min_count=None, max_count=max_num)
    # 使用消息本身带有的图片，如果本身不带图片则使用图片列表
    used_saved_list = False
    if not img_urls:
        img_urls = await get_image_list(ctx)
        used_saved_list = True
        assert_and_reply(img_urls, """
请指定要操作的图片！
方法1. 回复包含单张、多张图片的消息、折叠转发消息
方法2. 使用图片列表，请使用 /img push 回复包含图片以添加图片
""".strip())
        
    # 下载图片
    imgs = []
    for img_url in img_urls:
        try:
            img = await download_image(img_url)
        except Exception as e:
            logger.print_exc(f"获取图片 {img_url} 失败")
            await ctx.asend_reply_msg(f"获取图片 {img_url} 失败")
            raise NoReplyException()
        imgs.append(img)

    if len(imgs) == 1:
        return imgs[0], used_saved_list, list(img_urls) if used_saved_list else []
    return imgs, used_saved_list, list(img_urls) if used_saved_list else []

# 进行图片操作
async def operate_image(
    ctx: HandlerContext,
    initial_operation: str | None = None,
) -> Image.Image:
    """解析并执行图片操作链；裸快捷指令通过 initial_operation 补回首个操作名。"""
    args = ctx.get_args().strip().split()
    all_op_names = ImageOperation.all_ops.keys()
    if initial_operation is not None:
        assert_and_reply(
            initial_operation in all_op_names,
            f"未知图片操作 {initial_operation}, 可用的操作: {', '.join(all_op_names)}",
        )
        args.insert(0, initial_operation)
    assert_and_reply(args, f"""
操作序列不能为空！
使用方式: (回复一张图片) /img 操作1 参数1 操作2 参数2 ...
可用的操作: {', '.join(all_op_names)}
使用 /img help 操作名 获取某个操作的帮助
""".strip())

    # 获取操作和参数序列
    ops: List[Tuple[ImageOperation, List[str]]] = []
    for arg in args:
        if arg in all_op_names:
            ops.append((ImageOperation.all_ops[arg], []))
        else:
            assert_and_reply(ops, f"未指定初始操作, 可用的操作: {', '.join(all_op_names)}")
            ops[-1][1].append(arg)
    logger.info(f"请求图片操作\"{args}\" 序列: {[(op.name, args) for op, args in ops]}")

    assert_and_reply(ops, f"未指定操作, 可用的操作: {', '.join(all_op_names)}")
    assert_and_reply(len(ops) <= 10, f"操作过多, 最多支持10个操作")

    # 操作可对多图逐张批处理，声明类型无法完整表达这条动态链路。
    # 因此每一步都以实际返回对象重新校验，避免 Any 被误当成 Multiple，同时保留
    # “多图 resize 后 concat”这类合法组合。

    # 获取图片，并检查初始输入类型是否匹配
    img, used_saved_list, consumed_urls = await get_multi_images(ctx)
    img_num = 1 if isinstance(img, Image.Image) else len(img)
    img_type = ImageType.get_type(img)
    first_input_type = ops[0][0].input_type
    if img_num == 1:
        assert_and_reply(first_input_type.check_img(img), f"初始图片类型不匹配, 需要 {first_input_type}, 实际为 {img_type}")
    elif img_num > 1:
        if first_input_type != ImageType.Multiple:
            for i, item in enumerate(img):
                assert_and_reply(first_input_type.check_img(item), f"第{i+1}张图片类型不匹配, 需要 {first_input_type}, 实际为 {ImageType.get_type(item)}")

    # 执行操作序列
    for i, (op, args) in enumerate(ops):
        try:
            img = await run_in_pool(op, img, args, pool=IMGTOOL_EXECUTOR)
        except ImageOperationUnavailableError as e:
            raise ReplyException(str(e))
        except ReplyException:
            raise
        except Exception as e:
            logger.print_exc(f"执行第{i+1}个图片操作 {op.name} 失败")
            raise ReplyException(f"执行第{i+1}个图片操作 {op.name} 失败: {e}")
        
    logger.info(f"{len(ops)}个图片操作全部执行完毕")

    if isinstance(img, list):
        msgs = [f"{await get_image_cq(item)}#{i}" for i, item in enumerate(img, start=1)]
        send_result = await ctx.asend_fold_msg(msgs)
    else:
        send_result = await ctx.asend_reply_msg(await get_image_cq(img))

    # 结果成功发出后才消费本次快照；回复其他图片不会影响保存列表，发送失败也可重试。
    if used_saved_list:
        await consume_image_list_snapshot(ctx, consumed_urls)
    return send_result


async def run_image_operation(
    ctx: HandlerContext,
    initial_operation: str | None = None,
) -> Image.Image:
    """在统一的用户互斥锁和重型任务槽位中执行一次图片操作链。"""
    await ctx.block(f"{ctx.user_id}", 5)
    async with imgtool_job_slot():
        return await operate_image(ctx, initial_operation)


# 图片操作Handler
img_op = CmdHandler(["/img", "/imgtool"], logger, priority=1)
img_op.check_cdrate(cd).check_wblist(gbl)
@img_op.handle()
async def _(ctx: HandlerContext):
    await run_image_operation(ctx)

# push图片列表Handler
img_push = CmdHandler(["/img push", "/imgpush"], logger, priority=1)
img_push.check_cdrate(cd).check_wblist(gbl)
@img_push.handle()
async def _(ctx: HandlerContext):
    await add_image_to_list(ctx)

# pop图片列表Handler
img_pop = CmdHandler(["/img pop", "/imgpop"], logger, priority=1)
img_pop.check_cdrate(cd).check_wblist(gbl)
@img_pop.handle()
async def _(ctx: HandlerContext):
    await pop_image_from_list(ctx)

# 清空图片列表Handler
img_clear = CmdHandler(["/img clear", "/imgclear"], logger, priority=1)
img_clear.check_cdrate(cd).check_wblist(gbl)
@img_clear.handle()
async def _(ctx: HandlerContext):
    await clear_image_list(ctx)

# 翻转图片列表Handler
img_reverse = CmdHandler(["/img rev", "/imgrev"], logger, priority=1)
img_reverse.check_cdrate(cd).check_wblist(gbl)
@img_reverse.handle()
async def _(ctx: HandlerContext):
    await reverse_image_list(ctx)

# 图片操作帮助handler
img_help = CmdHandler(["/img help", "/imghelp", "/imgh"], logger, priority=1)
img_help.check_cdrate(cd).check_wblist(gbl)
@img_help.handle()
async def _(ctx: HandlerContext):
    ops = ImageOperation.all_ops
    op_name = ctx.get_args().strip()
    assert_and_reply(op_name, f"请输入要查找帮助的操作名，可用的操作: {', '.join(ops.keys())}")
    op = ops.get(op_name)
    assert_and_reply(op, f"未找到操作 {op_name}, 可用的操作: {', '.join(ops.keys())}")
    msg = f"【{op.name}】\n"
    msg += f"{op.input_type} -> {op.output_type}\n"
    msg += op.help
    return await ctx.asend_reply_msg(msg.strip())


# ============================= 图片操作 ============================= # 

class GifOperation(ImageOperation):
    def __init__(self):
        super().__init__("gif", ImageType.Static, ImageType.Static, 'single')
        self.help = """
将静态PNG图片转换为GIF，让透明部分能够在聊天中正确显示，使用方式:
gif n 使用普通算法生成GIF
gif 使用优化算法以默认50%不透明度阈值生成GIF
gif 0.8 使用优化算法以80%不透明度阈值生成GIF
""".strip()

    def parse_args(self, args: List[str]) -> dict:
        ret = { 'opt': True, 'threshold': 0.5 }
        assert_and_reply(len(args) <= 1, "最多只支持一个参数")
        if len(args) == 1:
            if args[0] == 'n':
                ret['opt'] = False
            else:
                ret['threshold'] = float(args[0])
                assert_and_reply(0.0 <= ret['threshold'] <= 1.0, "不透明度阈值必须在0-1之间")
        return ret

    def operate(self, img: Image.Image, args: dict=None, image_type: ImageType=None, frame_idx: int=0, total_frame: int=1) -> Image.Image:
        if is_animated(img):
            return img
        with TempFilePath("gif") as tmp_path:
            if args['opt']:
                save_transparent_static_gif(img, tmp_path, args['threshold'])
            else:
                img.convert('RGBA').save(tmp_path, save_all=True, append_images=[], duration=0, loop=0)
            return open_image(tmp_path)

class PngOperation(ImageOperation):
    def __init__(self):
        super().__init__("png", ImageType.Static, ImageType.Static, 'batch')
        self.help = "将图片转换为png格式"

    def parse_args(self, args: List[str]) -> dict:
        assert_and_reply(not args, "该操作不接受参数")
        return None
    
    def operate(self, img: Image.Image, args: dict=None, image_type: ImageType=None, frame_idx: int=0, total_frame: int=1) -> Image.Image:
        img = img.convert('RGBA')
        return img

class ResizeOperation(ImageOperation):
    def __init__(self):
        super().__init__("resize", ImageType.Any, ImageType.Any, 'batch')
        self.help = """
缩放图像，使用方式:
resize 256 128: 缩放到256x128
resize 256: 保持宽高比缩放到长边为256
resize 0.5x: 保持宽高比缩放到原图50%
resize 3.0x 2.0x: 宽缩放3倍高缩放2倍
""".strip()

    def parse_args(self, args: List[str]) -> dict:
        ret = {
            'w_scale': None,
            'h_scale': None,
            'w': None,
            'h': None,
            'max': None,
        }
        if len(args) == 1:
            if args[0].endswith('x'):
                ret['w_scale'] = float(args[0].removesuffix('x'))
                ret['h_scale'] = float(args[0].removesuffix('x'))
            else:
                ret['max'] = int(args[0])
        elif len(args) == 2:
            if args[0].endswith('x'):
                ret['w_scale'] = float(args[0].removesuffix('x'))
            else:
                ret['w'] = int(args[0])
            if args[1].endswith('x'):
                ret['h_scale'] = float(args[1].removesuffix('x'))
            else:
                ret['h'] = int(args[1])
        else:
            raise Exception()
        return ret

    def operate(self, img: Image.Image, args: dict, image_type: ImageType=None, frame_idx: int=0, total_frame: int=1) -> Image.Image:
        w, h = img.size
        if args['max'] is not None:
            if w > h:
                h = int(args['max'] * h / w)
                w = args['max']
            else:
                w = int(args['max'] * w / h)
                h = args['max']
        else:
            if args['w_scale'] is not None:
                w = int(w * args['w_scale'])
            if args['h_scale'] is not None:
                h = int(h * args['h_scale'])
            if args['w'] is not None:
                w = args['w']
            if args['h']is not None:
                h = args['h']
        assert_and_reply(0 < w * h * total_frame <= 1024 * 1024 * 16, f"图片尺寸{w}x{h}超出限制")
        return img.resize((w, h), Image.Resampling.BILINEAR)

class MirrorOperation(ImageOperation):
    def __init__(self):
        super().__init__("mirror", ImageType.Any, ImageType.Any, 'batch')
        self.help = """
镜像翻转，使用方式:
mirror: 水平镜像
mirror v: 垂直镜像
""".strip()
        
    def parse_args(self, args: List[str]) -> dict:
        assert_and_reply(len(args) <= 1, "最多只支持一个参数")
        assert_and_reply(not args or args == ['v'], "参数只能是 v（垂直镜像）")
        if args == ['v']:
            return {'mode': 'v'}
        return {'mode': 'h'}
    
    def operate(self, img: Image.Image, args: dict, image_type: ImageType=None, frame_idx: int=0, total_frame: int=1) -> Image.Image:
        if args['mode'] == 'h':
            return img.transpose(Image.FLIP_LEFT_RIGHT)
        else:
            return img.transpose(Image.FLIP_TOP_BOTTOM)

class RotateOperation(ImageOperation):
    def __init__(self):
        super().__init__("rotate", ImageType.Any, ImageType.Any, 'batch')
        self.help = """
旋转图像，使用方式:
rotate 90: 逆时针旋转90度
""".strip()
        
    def parse_args(self, args: List[str]) -> dict:
        assert_and_reply(len(args) == 1, "需要一个角度参数")
        return {'degree': int(args[0])}
    
    def operate(self, img: Image.Image, args: dict, image_type: ImageType=None, frame_idx: int=0, total_frame: int=1) -> Image.Image:
        return img.rotate(args['degree'], expand=True)

class BackOperation(ImageOperation):
    def __init__(self):
        super().__init__("back", ImageType.Animated, ImageType.Animated, 'single')
        self.help = "将动图在时间上反向播放"

    def parse_args(self, args: List[str]) -> dict:
        assert_and_reply(not args, "该操作不接受参数")
        return None
    
    def operate(self, img: Image.Image, args: dict=None, image_type: ImageType=None, frame_idx: int=0, total_frame: int=1) -> Image.Image:
        frames, durations = get_gif_timeline(img)
        frames.reverse()
        durations.reverse()
        return frames_to_gif(frames, quantize_gif_durations(durations))


GIF_PLAYBACK_MIN_FRAME_DURATION_MS = 20


def _build_speed_timeline(
    frames: List[Image.Image],
    durations: List[int],
    args: dict,
) -> tuple[List[Image.Image], List[int]]:
    """应用一次倍速并按 GIF 时间精度重采样，保证输出总时长不被重复缩短。"""
    if 'speed' in args:
        desired_durations = [duration / args['speed'] for duration in durations]
    else:
        desired_durations = [float(args['duration'])] * len(frames)

    target_duration = sum(desired_durations)
    target_ticks = max(1, round(target_duration / GIF_TIME_UNIT_MS))
    max_playable_frames = target_ticks // (
        GIF_PLAYBACK_MIN_FRAME_DURATION_MS // GIF_TIME_UNIT_MS
    )
    target_frame_count = min(
        len(frames),
        ANIMATED_OUTPUT_MAX_FRAMES,
        max_playable_frames,
    )
    if target_frame_count < 2:
        if 'speed' in args:
            max_rate = sum(durations) / (GIF_PLAYBACK_MIN_FRAME_DURATION_MS * 2)
            raise ReplyException(f"加速倍率过大！该图像最多只能加速{max_rate:.2f}倍")
        raise ReplyException("帧间隔过短，无法生成至少两帧的有效动图")

    if target_frame_count < len(frames):
        frames, desired_durations = resample_gif_timeline(
            frames,
            desired_durations,
            target_frame_count,
        )
    quantized_durations = quantize_gif_durations(
        desired_durations,
        min_duration_ms=GIF_PLAYBACK_MIN_FRAME_DURATION_MS,
    )
    if args.get('back', False):
        frames.reverse()
        quantized_durations.reverse()
    return frames, quantized_durations


class SpeedOperation(ImageOperation):
    def __init__(self):
        super().__init__("speed", ImageType.Animated, ImageType.Animated, 'single')
        self.help = """
调整动图播放速度，使用方式:
speed 2.0x 设置动图播放速度为原图的2倍
speed -2.0x 设置动图播放速度为原图的2倍倒放
speed 100 设置动图帧间隔为100ms
""".strip()
        
    def parse_args(self, args: List[str]) -> dict:
        assert_and_reply(len(args) == 1, "需要一个速度参数")
        ret = {}
        if args[0].endswith('x'): 
            ret['speed'] = float(args[0].removesuffix('x'))
            if ret['speed'] < 0:
                ret['back'] = True
                ret['speed'] = -ret['speed']
            assert_and_reply(0.01 <= ret['speed'] <= 100.0, "加速倍率必须在0.01-100.0之间")
        else: 
            ret['duration'] = int(args[0])
            if ret['duration'] < 0:
                ret['back'] = True
                ret['duration'] = -ret['duration']
            assert_and_reply(1 <= ret['duration'] <= 1000, "帧间隔必须在1ms-1000ms之间")
        return ret
        
    def operate(self, img: Image.Image, args: dict, image_type: ImageType=None, frame_idx: int=0, total_frame: int=1) -> Image.Image:
        frames, durations = get_gif_timeline(img)
        frames, durations = _build_speed_timeline(frames, durations, args)
        return frames_to_gif(frames, durations)


class GrayOperation(ImageOperation):
    def __init__(self):
        super().__init__("gray", ImageType.Any, ImageType.Any, 'batch')
        self.help = "将图片转换为灰度图"
    
    def parse_args(self, args: List[str]) -> dict:
        assert_and_reply(not args, "该操作不接受参数")
        return None
    
    def operate(self, img: Image.Image, args: dict=None, image_type: ImageType=None, frame_idx: int=0, total_frame: int=1) -> Image.Image:
        return img.convert('L')
    
class MidOperation(ImageOperation):
    def __init__(self):
        super().__init__("mid", ImageType.Any, ImageType.Any, 'batch')
        self.help = """将图片的一侧对称贴到另一侧，使用方式:
mid: 左侧贴到右侧
mid r: 右侧贴到左侧
mid v: 上侧贴到下侧
mid v r: 下侧贴到上侧
""".strip()
        
    def parse_args(self, args: List[str]) -> dict:
        assert_and_reply(len(args) <= 2, "最多只支持两个参数")
        assert_and_reply(all(arg in {'v', 'r'} for arg in args), "参数只能是 v 或 r")
        assert_and_reply(len(args) == len(set(args)), "参数不能重复")
        ret = {}
        if 'v' in args: ret['mode'] = 'v'
        else: ret['mode'] = 'h'
        if 'r' in args: ret['mode'] += 'r'
        return ret

    def operate(self, img: Image.Image, args: dict, image_type: ImageType=None, frame_idx: int=0, total_frame: int=1) -> Image.Image:
        width, height = img.size
        mode = args['mode']
        if mode == "h":
            source_width = (width + 1) // 2
            left_img = img.crop((0, 0, source_width, height))
            right_img = left_img.transpose(Image.FLIP_LEFT_RIGHT)
            new_img = Image.new("RGBA", (width, height))
            new_img.paste(left_img, (0, 0))
            new_img.paste(right_img, (width - source_width, 0))
        elif mode == "v":
            source_height = (height + 1) // 2
            top_img = img.crop((0, 0, width, source_height))
            bottom_img = top_img.transpose(Image.FLIP_TOP_BOTTOM)
            new_img = Image.new("RGBA", (width, height))
            new_img.paste(top_img, (0, 0))
            new_img.paste(bottom_img, (0, height - source_height))
        elif mode == "hr":
            source_width = (width + 1) // 2
            right_img = img.crop((width - source_width, 0, width, height))
            left_img = right_img.transpose(Image.FLIP_LEFT_RIGHT)
            new_img = Image.new("RGBA", (width, height))
            new_img.paste(left_img, (0, 0))
            new_img.paste(right_img, (width - source_width, 0))
        else:
            source_height = (height + 1) // 2
            bottom_img = img.crop((0, height - source_height, width, height))
            top_img = bottom_img.transpose(Image.FLIP_TOP_BOTTOM)
            new_img = Image.new("RGBA", (width, height))
            new_img.paste(top_img, (0, 0))
            new_img.paste(bottom_img, (0, height - source_height))
        return new_img

class InvertOperation(ImageOperation):
    def __init__(self):
        super().__init__("invert", ImageType.Any, ImageType.Any, 'batch')
        self.help = "将图片颜色反转"

    def parse_args(self, args: List[str]) -> dict:
        assert_and_reply(not args, "该操作不接受参数")
        return None

    def operate(self, img: Image.Image, args: dict=None, image_type: ImageType=None, frame_idx: int=0, total_frame: int=1) -> Image.Image:
        img = img.convert('RGB')
        return ImageOps.invert(img)

class RepeatOperation(ImageOperation): 
    def __init__(self):
        super().__init__("repeat", ImageType.Any, ImageType.Any, 'batch')
        self.help = """
将图片重复多次，使用方式:
repeat 2 3: 横向重复2次，纵向重复3次
repeat 1 2: 只纵向重复2次
""".strip()
        
    def parse_args(self, args: List[str]) -> dict:
        assert_and_reply(len(args) == 2, "需要两个参数")
        ret = {'w': int(args[0]), 'h': int(args[1])}
        assert_and_reply(1 <= ret['w'] <= 10 and 1 <= ret['h'] <= 10, "重复次数只能在1-10之间")
        return ret
    
    def operate(self, img: Image.Image, args: dict, image_type: ImageType=None, frame_idx: int=0, total_frame: int=1) -> Image.Image:
        w_times, h_times = args['w'], args['h']
        width, height = img.size
        size_limit = 512
        if max(width * w_times, height * h_times) <= size_limit:
            width = width * w_times
            height = height * h_times
        else:
            if width * w_times > height * h_times:
                height = size_limit * height * h_times // (width * w_times)
                width = size_limit
            else:
                width = size_limit * width * w_times // (height * h_times)
                height = size_limit
        small_width, small_height = width // w_times, height // h_times
        img = img.resize((small_width, small_height)).convert('RGBA')
        new_img = Image.new("RGBA", (small_width * w_times, small_height * h_times))
        for i in range(w_times):
            for j in range(h_times):
                new_img.paste(img, (i * small_width, j * small_height), img)
        return new_img

def _parse_motion_args(args: List[str], operation_name: str) -> dict:
    """解析 fan/flow 共用参数，拒绝未知或重复的简写。"""
    flags = {'r'} if operation_name == 'fan' else {'v', 'r'}
    assert_and_reply(len(args) <= len(flags) + 1, "参数过多")
    seen_flags = set()
    speed = 1.0
    speed_seen = False
    for arg in args:
        if arg in flags:
            assert_and_reply(arg not in seen_flags, f"参数 {arg} 不能重复")
            seen_flags.add(arg)
        elif arg.endswith('x'):
            assert_and_reply(not speed_seen, "速度参数只能指定一次")
            speed = float(arg.removesuffix('x'))
            speed_seen = True
        else:
            allowed = '、'.join(sorted(flags))
            raise ValueError(f"未知参数 {arg}，仅支持 {allowed} 和速度倍率（如 2x）")
    assert_and_reply(0.2 <= speed <= 5.0, "速度只能在0.2-5.0之间")
    return {'flags': seen_flags, 'speed': speed}


def _render_motion_effect(img: Image.Image, args: dict, effect: str) -> Image.Image:
    """生成旋转或平移动图；输入为动图时同步保留原动画内容。"""
    animated_source = is_animated(img)
    effect_frame_count = max(4, math.ceil(20 / args['speed']))
    if animated_source:
        source_frames, source_durations = get_gif_timeline(img)
        total_duration = sum(source_durations)
        max_timeline_frames = max(2, round(total_duration / GIF_TIME_UNIT_MS))
        frame_count = min(
            ANIMATED_OUTPUT_MAX_FRAMES,
            max_timeline_frames,
            max(len(source_frames), effect_frame_count),
        )
        source_frames, output_durations = resample_gif_timeline(
            source_frames,
            source_durations,
            frame_count,
        )
        output_durations = quantize_gif_durations(output_durations)
    else:
        frame_count = min(ANIMATED_OUTPUT_MAX_FRAMES, effect_frame_count)
        source_frames = [img.copy() for _ in range(frame_count)]
        output_durations = [GIF_PLAYBACK_MIN_FRAME_DURATION_MS] * frame_count

    width, height = source_frames[0].size
    safe_width, safe_height = _fit_uniform_size(
        width,
        height,
        frame_count,
        ANIMATED_OUTPUT_PIXEL_LIMIT,
    )
    if (safe_width, safe_height) != (width, height):
        logger.info(
            f"{effect}预计输出超限，输入已缩放 {width}x{height} -> {safe_width}x{safe_height}"
        )
    source_frames = [
        frame.convert('RGBA').resize((safe_width, safe_height), Image.Resampling.LANCZOS)
        for frame in source_frames
    ]

    output_frames = []
    effect_cycle_ms = 400 / args['speed']
    elapsed_ms = 0
    for index, (source_frame, frame_duration) in enumerate(zip(source_frames, output_durations)):
        sample_time_ms = elapsed_ms + frame_duration / 2
        phase = (
            (sample_time_ms / effect_cycle_ms) % 1.0
            if animated_source
            else index / frame_count
        )
        elapsed_ms += frame_duration

        output = Image.new('RGBA', (safe_width, safe_height), (0, 0, 0, 0))
        if effect == 'fan':
            angle = 360 * phase
            if 'r' not in args['flags']:
                angle = -angle
            rotated = source_frame.rotate(angle, resample=Image.Resampling.BICUBIC, expand=False)
            output.alpha_composite(rotated)
        else:
            vertical = 'v' in args['flags']
            reverse = 'r' in args['flags']
            distance = safe_height if vertical else safe_width
            offset = int(phase * distance)
            if reverse:
                offset = distance - offset
            if vertical:
                output.alpha_composite(source_frame, (0, offset))
                output.alpha_composite(source_frame, (0, offset - distance))
            else:
                output.alpha_composite(source_frame, (offset, 0))
                output.alpha_composite(source_frame, (offset - distance, 0))
        output_frames.append(output)

    return frames_to_gif(output_frames, output_durations)


class FanOperation(ImageOperation):
    def __init__(self):
        super().__init__("fan", ImageType.Any, ImageType.Animated, 'single')
        self.help = """
大风车一张图片，使用方式:
fan: 顺时针旋转
fan r: 逆时针旋转
fan 2x: 旋转速度为2倍
fan r 0.5x: 逆时针旋转，旋转速度为0.5倍
"""

    def parse_args(self, args: List[str]) -> dict:
        return _parse_motion_args(args, 'fan')
    
    def operate(self, img: Image.Image, args: dict, image_type: ImageType=None, frame_idx: int=0, total_frame: int=1) -> Image.Image:
        return _render_motion_effect(img, args, 'fan')

class FlowOperation(ImageOperation):
    def __init__(self):
        super().__init__("flow", ImageType.Any, ImageType.Animated, 'single')
        self.help = """
添加平移流动效果，使用方式:
flow: 从左到右流动
flow v: 从上到下流动
flow r: 从右到左流动
flow v r: 从下到上流动
flow 2x: 流动速度为2倍
"""

    def parse_args(self, args: List[str]) -> dict:
        return _parse_motion_args(args, 'flow')
    
    def operate(self, img: Image.Image, args: dict, image_type: ImageType=None, frame_idx: int=0, total_frame: int=1) -> Image.Image:
        return _render_motion_effect(img, args, 'flow')

def _estimate_concat_dimensions(images: List[Image.Image], mode: str) -> tuple[int, int]:
    """按 concat_images 的布局规则估算画布尺寸。"""
    if mode == 'v':
        width = max(image.width for image in images)
        height = sum(max(1, int(image.height * width / image.width)) for image in images)
        return width, height
    if mode == 'h':
        height = max(image.height for image in images)
        width = sum(max(1, int(image.width * height / image.height)) for image in images)
        return width, height

    max_width = max(image.width for image in images)
    max_height = max(image.height for image in images)
    columns = max(1, int(math.sqrt(len(images))))
    rows = math.ceil(len(images) / columns)
    return max_width * columns, max_height * rows


def _fit_concat_inputs(images: List[Image.Image], mode: str) -> List[Image.Image]:
    """在创建拼接画布前缩放输入，避免先分配超大画布再补救。"""
    fitted = images
    for _ in range(4):
        width, height = _estimate_concat_dimensions(fitted, mode)
        if width * height <= STATIC_OUTPUT_PIXEL_LIMIT:
            return fitted
        scale = math.sqrt(STATIC_OUTPUT_PIXEL_LIMIT / (width * height)) * 0.995
        fitted = [
            image.resize(
                (max(1, int(image.width * scale)), max(1, int(image.height * scale))),
                Image.Resampling.LANCZOS,
            )
            for image in fitted
        ]
    raise ReplyException("拼接后的图片尺寸过大，无法安全处理")


class ConcatOperation(ImageOperation):
    def __init__(self):
        super().__init__("concat", ImageType.Multiple, ImageType.Static, 'single')
        self.help = """
将多张图片拼接成一张，使用方式:
concat: 垂直拼接
concat h: 水平拼接
concat g: 网格拼接
""".strip()
        
    def parse_args(self, args: List[str]) -> dict:
        assert_and_reply(len(args) <= 1, "最多只支持一个参数")
        ret = {'mode': 'v'}
        if args:
            assert_and_reply(args[0] in {'v', 'h', 'g'}, "参数只能是 v、h 或 g")
            ret['mode'] = args[0]
        return ret
    
    def operate(self, imgs: List[Image.Image], args: dict, image_type: ImageType=None, frame_idx: int=0, total_frame: int=1) -> Image.Image:
        imgs = _fit_concat_inputs(imgs, args['mode'])
        img = concat_images(imgs, args['mode'])
        return img

class StackOperation(ImageOperation):
    def __init__(self):
        super().__init__("stack", ImageType.Multiple, ImageType.Animated, 'single')
        self.help = """
将多张图片堆叠成动图，所有图片会缩放到和第一张图相同大小，使用方式:
stack: 默认以fps为20堆叠
stack 10: 以fps为10堆叠
""".strip()

    def parse_args(self, args: List[str]) -> dict:
        assert_and_reply(len(args) <= 1, "最多只支持一个参数")
        ret = {'fps': 20}
        if args:
            ret['fps'] = int(args[0])
        assert_and_reply(1 <= ret['fps'] <= 50, "fps只能在1-50之间")
        return ret
    
    def operate(self, imgs: List[Image.Image], args: dict, image_type: ImageType=None, frame_idx: int=0, total_frame: int=1) -> Image.Image:
        fps = args['fps']
        frame_count = len(imgs)
        assert_and_reply(frame_count <= ANIMATED_OUTPUT_MAX_FRAMES, f"最多只能堆叠{ANIMATED_OUTPUT_MAX_FRAMES}张图片")
        w, h = imgs[0].size
        w, h = _fit_uniform_size(w, h, frame_count, ANIMATED_OUTPUT_PIXEL_LIMIT)
        frames = [img.resize((w, h), Image.Resampling.LANCZOS) for img in imgs]
        durations = quantize_gif_durations(
            [1000 / fps] * frame_count,
            min_duration_ms=GIF_PLAYBACK_MIN_FRAME_DURATION_MS,
        )
        return frames_to_gif(frames, durations)

class ExtractOperation(ImageOperation):
    def __init__(self):
        super().__init__("extract", ImageType.Animated, ImageType.Multiple, 'single')
        self.help = """
将动图拆分成多张图片，使用方式:
extract: 拆分动图，帧数太多会自动抽帧
extract 2: 以间隔2帧拆分
""".strip()
        
    def parse_args(self, args: List[str]) -> dict:
        assert_and_reply(len(args) <= 1, "最多只支持一个参数")
        ret = {'interval': None }
        if args:
            ret['interval'] = int(args[0])
            assert_and_reply(1 <= ret['interval'] <= 100, "间隔只能在1-100之间")
        return ret
    
    def operate(self, img: Image.Image, args: dict, image_type: ImageType=None, frame_idx: int=0, total_frame: int=1) -> List[Image.Image]: 
        interval = args['interval']
        frames = gif_to_frames(img)
        n_frames = len(frames)
        max_frame_num = 32
        if interval: 
            if interval >= n_frames:
                raise ReplyException(f"拆分间隔过大！该动图最多只能以{n_frames}帧拆分")
            frame_num = math.ceil(n_frames / interval)
            if frame_num > max_frame_num:
                min_interval = math.ceil(n_frames / max_frame_num)
                raise ReplyException(f"拆分间隔过小！该动图最多只能以{min_interval}帧拆分")
        else:
            interval = max(1, math.ceil(n_frames / max_frame_num))
        return [frames[i] for i in range(0, n_frames, interval)]

class MirageOperation(ImageOperation):
    def __init__(self):
        super().__init__("mirage", ImageType.Multiple, ImageType.Static, 'single')
        self.help = """
生成幻影坦克图片，使用方式:
mirage: 使用列表中倒数第二张图片作为表面图，倒数第一张图片作为隐藏图
mirage r: 使用列表中倒数第一张图片作为表面图，倒数第二张图片作为隐藏图
""".strip()
        
    def parse_args(self, args: List[str]) -> dict:
        assert_and_reply(len(args) <= 1, "最多只支持一个参数")
        assert_and_reply(not args or args == ['r'], "参数只能是 r（交换表图和底图）")
        ret = {'rev': False}
        if args == ['r']: ret['rev'] = True
        return ret
    
    def operate(self, img: List[Image.Image], args: dict, image_type: ImageType=None, frame_idx: int=0, total_frame: int=1) -> Image.Image:
        assert_and_reply(len(img) >= 2, "至少需要两张图片")
        if args['rev']:
            surface = img[-1]
            hidden = img[-2]
        else:
            surface = img[-2]
            hidden = img[-1]
        target_width = max(surface.width, hidden.width)
        target_height = max(
            max(1, int(surface.height * target_width / surface.width)),
            max(1, int(hidden.height * target_width / hidden.width)),
        )
        safe_width, safe_height = _fit_uniform_size(
            target_width,
            target_height,
            1,
            STATIC_OUTPUT_PIXEL_LIMIT,
        )
        if safe_width != target_width:
            scale = safe_width / target_width
            surface = surface.resize(
                (max(1, int(surface.width * scale)), max(1, int(surface.height * scale))),
                Image.Resampling.LANCZOS,
            )
            hidden = hidden.resize(
                (max(1, int(hidden.width * scale)), max(1, int(hidden.height * scale))),
                Image.Resampling.LANCZOS,
            )
        return generate_mirage(surface, hidden)

class BrightenOperation(ImageOperation):
    def __init__(self):
        super().__init__("brighten", ImageType.Any, ImageType.Any, 'batch')
        self.help = """
调整图片亮度，使用方式:
brighten 1.5: 调整图片亮度为1.5倍
brighten 0.5: 调整图片亮度为0.5倍
0.0对应黑色图像，1.0对应原图像
""".strip()
        
    def parse_args(self, args: List[str]) -> dict:
        assert_and_reply(len(args) == 1, "需要一个参数")
        ret = {'ratio': float(args[0])}
        assert_and_reply(0.0 <= ret['ratio'] <= 100.0, "亮度参数只能在0.0-100.0之间")
        return ret  
    
    def operate(self, img: Image.Image, args: dict, image_type: ImageType=None, frame_idx: int=0, total_frame: int=1) -> Image.Image:
        ratio = args['ratio']
        img = img.convert('RGBA')
        return ImageEnhance.Brightness(img).enhance(ratio)
    
class ContrastOperation(ImageOperation):
    def __init__(self):
        super().__init__("contrast", ImageType.Any, ImageType.Any, 'batch')
        self.help = """
调整图片对比度，使用方式:
contrast 1.5: 调整图片对比度为1.5倍
contrast 0.5: 调整图片对比度为0.5倍
0.0对应纯灰图像，1.0对应原图像
""".strip()
        
    def parse_args(self, args: List[str]) -> dict:
        assert_and_reply(len(args) == 1, "需要一个参数")
        ret = {'ratio': float(args[0])}
        assert_and_reply(0.0 <= ret['ratio'] <= 100.0, "对比度参数只能在0.0-100.0之间")
        return ret  
    
    def operate(self, img: Image.Image, args: dict, image_type: ImageType=None, frame_idx: int=0, total_frame: int=1) -> Image.Image:
        ratio = args['ratio']
        img = img.convert('RGBA')
        return ImageEnhance.Contrast(img).enhance(ratio)
    
class SharpenOperation(ImageOperation):
    def __init__(self):
        super().__init__("sharpen", ImageType.Any, ImageType.Any, 'batch')
        self.help = """
调整图片锐度，使用方式:
sharpen 1.5: 调整图片锐度为1.5倍
sharpen 0.5: 调整图片锐度为0.5倍
0.0对应模糊图像，1.0对应原图像，2.0对应锐化图像
""".strip()
        
    def parse_args(self, args: List[str]) -> dict:
        assert_and_reply(len(args) == 1, "需要一个参数")
        ret = {'ratio': float(args[0])}
        assert_and_reply(0.0 <= ret['ratio'] <= 100.0, "锐度参数只能在0.0-100.0之间")
        return ret  
    
    def operate(self, img: Image.Image, args: dict, image_type: ImageType=None, frame_idx: int=0, total_frame: int=1) -> Image.Image:
        ratio = args['ratio']
        img = img.convert('RGBA')
        return ImageEnhance.Sharpness(img).enhance(ratio)
        
class SaturateOperation(ImageOperation):
    def __init__(self):
        super().__init__("saturate", ImageType.Any, ImageType.Any, 'batch')
        self.help = """
调整图片饱和度，使用方式:
saturate 1.5: 调整图片饱和度为1.5倍
saturate 0.5: 调整图片饱和度为0.5倍
0.0对应黑白图像，1.0对应原图像
""".strip()
        
    def parse_args(self, args: List[str]) -> dict:
        assert_and_reply(len(args) == 1, "需要一个参数")
        ret = {'ratio': float(args[0])}
        assert_and_reply(0.01 <= ret['ratio'] <= 100.0, "饱和度参数只能在0.01-100.0之间")
        return ret  

    def operate(self, img: Image.Image, args: dict, image_type: ImageType=None, frame_idx: int=0, total_frame: int=1) -> Image.Image:
        ratio = args['ratio']
        img = img.convert('RGBA')
        return ImageEnhance.Color(img).enhance(ratio)

class BlurOperation(ImageOperation):
    def __init__(self):
        super().__init__("blur", ImageType.Any, ImageType.Any, 'batch')
        self.help = """
对图片进行模糊处理，使用方式:
blur 对图片应用默认半径为3的高斯模糊
blur 5 对图片应用半径为5的高斯模糊
""".strip()

    def parse_args(self, args: List[str]) -> dict:
        assert_and_reply(len(args) <= 1, "最多只支持一个参数")
        ret = {'radius': 3}
        if args:
            ret['radius'] = int(args[0])
        assert_and_reply(1 <= ret['radius'] <= 32, "模糊半径只能在1-32之间")
        return ret
    
    def operate(self, img: Image.Image, args: dict, image_type: ImageType=None, frame_idx: int=0, total_frame: int=1) -> Image.Image:
        radius = args['radius']
        img = img.convert('RGBA')
        return img.filter(ImageFilter.GaussianBlur(radius=radius))

class CropOperation(ImageOperation):
    def __init__(self):
        super().__init__("crop", ImageType.Any, ImageType.Any, 'batch')
        self.help = """
裁剪图片，使用方式:
crop 100x100: 裁剪图片100x100中间部分
crop 0.5x0.5: 裁剪图片中心长宽为原来50%的部分
crop 50%x100: 裁剪图片中心长为原来50%，宽为100px的部分
crop 100x100 l: 裁剪图片100x100左边部分(lrtb:左右上下)
crop 100x100 lt: 裁剪图片100x100左上角部分
crop 100x100 50x50: 裁剪图片100x100，相对左上角偏移(50,50)px
crop l0.1 t0.2 裁剪掉图片左边10%，上边20%部分
参数中使用实数（带有小数点）或者百分比则对应比例，使用整数则对应像素
""".strip()

    def parse_args(self, args: List[str]) -> dict:
        assert_and_reply(len(args) >= 1, "至少需要一个参数")
        assert_and_reply(len(args) <= 4, "最多只支持四个参数")

        def s_to_i_or_f(s):
            if '.' in s:
                return float(s)
            elif '%' in s:
                return float(s.replace('%', '')) / 100.0
            else:
                return int(s)
            
        ret = {}
        if 'x' in args[0]:
            ret['type'] = 1
            ret['size'] = tuple(map(s_to_i_or_f, args[0].split('x')))
            if len(args) == 2:
                if 'x' in args[1]:
                    ret['offset'] = tuple(map(s_to_i_or_f, args[1].split('x')))
                else:
                    assert_and_reply(args[1] in ALIGN_MAP, f"指定位置错误，必须是{ALIGN_MAP.keys()}中的一个")
                    ret['align'] = args[1].strip()
        else:
            ret['type'] = 2
            ret['border'] = {}
            for arg in args:
                arg = arg.strip()
                assert_and_reply(arg[0] in 'lrtb', f"裁剪方向错误，必须是(l,r,t,b)中的一个")
                ret['border'][arg[0]] = s_to_i_or_f(arg[1:])
        return ret
    
    def operate(self, img: Image.Image, args: dict, image_type: ImageType=None, frame_idx: int=0, total_frame: int=1) -> Image.Image:
        w, h = img.size
        def getlen(l, ref):
            if isinstance(l, float):
                return int(l * ref)
            return l
        def getsize(size):
            return getlen(size[0], w), getlen(size[1], h)
            
        x1, y1, x2, y2, cw, ch = 0, 0, w, h, w, h
        if args['type'] == 1:
            cw, ch = getsize(args['size'])
            if 'offset' in args:
                x1, y1 = getsize(args['offset'])
            else:
                x1, y1 = crop_by_align((w, h), (cw, ch), args.get('align', 'c'))[:2]
            x2, y2 = x1 + cw, y1 + ch
        else:
            if 'l' in args['border']:
                x1 = getlen(args['border']['l'], w)
            if 'r' in args['border']:
                x2 = w - getlen(args['border']['r'], w)
            if 't' in args['border']:
                y1 = getlen(args['border']['t'], h)
            if 'b' in args['border']:
                y2 = h - getlen(args['border']['b'], h)
            cw, ch = x2 - x1, y2 - y1
        
        wh_str = f"({w}x{h})"
        bbox_str = f"[({x1},{y1})->({x2},{y2}) {cw}x{ch}]"
        assert_and_reply(x1 >= 0 and y1 >= 0 and x2 <= w and y2 <= h, f"裁剪区域{bbox_str}超出原图像{wh_str}")
        assert_and_reply(cw > 0, f"裁剪区域{bbox_str}宽度错误")
        assert_and_reply(ch > 0, f"裁剪区域{bbox_str}高度错误")

        return img.crop((x1, y1, x2, y2))

class DemirageOperation(ImageOperation):
    def __init__(self):
        super().__init__("demirage", ImageType.Static, ImageType.Multiple, 'single')
        self.help = """
提取幻影坦克图片的表图和底图
""".strip()
        
    def parse_args(self, args: List[str]) -> dict:
        assert_and_reply(not args, "该操作不接受参数")
        return None
    
    def operate(self, img: Image.Image, args: dict, image_type: ImageType=None, frame_idx: int=0, total_frame: int=1) -> List[Image.Image]: 
        surface = Image.new('RGBA', img.size, (255, 255, 255, 255))
        hidden = Image.new('RGBA', img.size, (0, 0, 0, 255))
        surface.paste(img, (0, 0), img)
        hidden.paste(img, (0, 0), img)
        surface = surface.convert('RGB')
        hidden = hidden.convert('RGB')
        return [surface, hidden]

class CutoutOperation(ImageOperation):
    def __init__(self):
        super().__init__("cutout", ImageType.Any, ImageType.Any, 'single')
        self.help = """
抠图，使用方式:
cutout: 使用洪水算法抠图（适合纯色背景），容差为默认20
cutout 50: 使用洪水算法抠图，容差为50
cutout ai: 使用AI模型抠图（当前服务器未启用）
""".strip()
        
    def parse_args(self, args: List[str]) -> dict:
        assert_and_reply(len(args) <= 2, "最多只支持两个参数")
        ret = {'method': 'floodfill', 'tolerance': 20 }
        method_seen = False
        tolerance_seen = False
        for arg in args:
            if arg in ['floodfill', 'ai']:
                assert_and_reply(not method_seen, "抠图算法只能指定一次")
                ret['method'] = arg
                method_seen = True
            elif arg.isdigit():
                assert_and_reply(not tolerance_seen, "容差值只能指定一次")
                ret['tolerance'] = int(arg)
                assert_and_reply(0 <= ret['tolerance'] <= 255, "容差值只能在0-255之间（默认容差为20）")
                tolerance_seen = True
            else:
                raise ValueError(f"未知参数 {arg}，仅支持 floodfill、ai 或 0-255 容差值")
        assert_and_reply(not (ret['method'] == 'ai' and tolerance_seen), "AI抠图不接受容差参数")
        method_limit = {
            'floodfill': parse_cfg_num(config.get('input_res_limit.cutout.floodfill')),
            'ai': parse_cfg_num(config.get('input_res_limit.cutout.ai')),
        }
        ret['_input_limit'] = method_limit[ret['method']]
        return ret
    
    def operate(self, img: Image.Image, args: dict=None, image_type: ImageType=None, frame_idx: int=0, total_frame: int=1) -> Image.Image:
        animated = is_animated(img)
        if animated:
            frames, durations = get_gif_timeline(img)
        else:
            frames = [img]

        if args['method'] == 'floodfill':
            frames = cutout_image(frames, args['tolerance']).image
        elif args['method'] == 'ai':
            try:
                from rembg import remove
            except ImportError as exc:
                raise ImageOperationUnavailableError(
                    "AI抠图组件当前未安装，请使用非AI抠图：/img cutout"
                ) from exc
            for i in range(len(frames)):
                frames[i] = remove(frames[i])

        if animated:
            return frames_to_gif(frames, quantize_gif_durations(durations))
        else:
            return frames[0]

class ShrinkOperation(ImageOperation):
    def __init__(self):
        super().__init__("shrink", ImageType.Any, ImageType.Any, 'single')
        self.help = """
裁剪透明，将图像边缘透明部分裁剪掉，使用方式:
shrink: 裁剪透明部分，默认透明度阈值为10（alpha小于等于阈值的像素被认为是透明）
shrink 50: 裁剪透明部分，透明度阈值为50
shrink 10 +10: 裁剪透明部分，透明度阈值为10，并在裁剪区域外扩展10像素
""".strip()
        
    def parse_args(self, args: List[str]) -> dict:
        ret = {'alpha_threshold': 10, 'edge': 0 }
        assert_and_reply(len(args) <= 2, "最多只支持两个参数")
        threshold_seen = False
        edge_seen = False
        for arg in args:
            if arg.startswith('+') and arg[1:].isdigit():
                assert_and_reply(not edge_seen, "扩展像素只能指定一次")
                ret['edge'] = int(arg[1:])
                assert_and_reply(0 <= ret['edge'] <= 100, "扩展像素只能在0-100之间")
                edge_seen = True
            elif arg.isdigit():
                assert_and_reply(not threshold_seen, "透明度阈值只能指定一次")
                ret['alpha_threshold'] = int(arg)
                assert_and_reply(0 <= ret['alpha_threshold'] <= 255, "透明度阈值只能在0-255之间（默认阈值为10）")
                threshold_seen = True
            else:
                raise ValueError(f"未知参数 {arg}，仅支持透明度阈值和 +扩展像素")
        return ret
    
    def operate(self, img: Image.Image, args: dict=None, image_type: ImageType=None, frame_idx: int=0, total_frame: int=1) -> Image.Image:
        animated = is_animated(img)
        if animated:
            frames, durations = get_gif_timeline(img)
        else:
            frames = [img]

        frames = shrink_image(frames, args['alpha_threshold'], args['edge']).image

        if animated:
            return frames_to_gif(frames, quantize_gif_durations(durations))
        else:
            return frames[0]

class BackgroundOperation(ImageOperation):
    def __init__(self):
        super().__init__("bg", ImageType.Any, ImageType.Any, 'batch')
        self.help = """
为图片添加背景色，使用方式:
bg: 使用默认白色作为背景颜色
bg 255 255 255: 使用RGB颜色值
bg #ff00ff: 使用颜色代码
""".strip()
        
    def parse_args(self, args: List[str]) -> dict:
        ret = {'color': (255, 255, 255) }
        assert_and_reply(not args or len(args) == 1 or len(args) == 3, "需要一个(颜色代码)或三个(RGB)参数")
        if len(args) == 1:
            ret['color'] = color_code_to_rgb(args[0])[:3]
        elif len(args) == 3:
            r = int(args[0])
            g = int(args[1])
            b = int(args[2])
            assert_and_reply(0 <= r <= 255 and 0 <= g <= 255 and 0 <= b <= 255, "RGB颜色值必须在0-255之间")
            ret['color'] = (r, g, b)
        return ret
    
    def operate(self, img: Image.Image, args: dict=None, image_type: ImageType=None, frame_idx: int=0, total_frame: int=1) -> Image.Image:
        bg = Image.new('RGBA', img.size, args['color'] + (255,))
        bg.alpha_composite(img.convert('RGBA'))
        return bg
            

# 注册所有图片操作
def register_all_ops():
    for name, obj in globals().items():
        if isinstance(obj, type) and issubclass(obj, ImageOperation) and obj != ImageOperation:
            obj()
register_all_ops()


# ================================ 裸操作快捷指令 ================================ #
# `/img` 始终是稳定入口；裸指令只提供快捷访问。若加载到此处时命令已被其他插件
# 占用，则跳过该快捷方式，用户仍可使用 `/img <操作>`。
DIRECT_IMAGE_OPERATION_RESERVED_COMMANDS = {"/gif"}


def _build_direct_image_operation_map() -> dict[str, str]:
    """生成可安全注册的“裸指令 -> 图片操作”映射，并避开已知命令冲突。"""
    occupied_commands = {
        command
        for handler in CmdHandler.cmd_handlers
        for command in handler.commands
    }
    operation_map = {}
    for operation_name in ImageOperation.all_ops:
        command = f"/{operation_name}"
        if command in DIRECT_IMAGE_OPERATION_RESERVED_COMMANDS:
            continue
        if command in occupied_commands:
            logger.warning(
                f"跳过图片操作快捷指令 {command}：该命令已被其他处理器占用，"
                f"请使用 /img {operation_name}"
            )
            continue
        operation_map[command] = operation_name
    return operation_map


DIRECT_IMAGE_OPERATION_MAP = _build_direct_image_operation_map()
direct_img_op = CmdHandler(
    list(DIRECT_IMAGE_OPERATION_MAP),
    logger,
    priority=1,
    disable_help=True,
)
direct_img_op.check_cdrate(cd).check_wblist(gbl)


@direct_img_op.handle()
async def _(ctx: HandlerContext):
    operation_name = DIRECT_IMAGE_OPERATION_MAP.get(ctx.trigger_cmd)
    assert_and_reply(
        operation_name,
        f"图片操作快捷指令 {ctx.trigger_cmd} 当前不可用，请改用 /img <操作>",
    )
    await run_image_operation(ctx, operation_name)


@on_startup()
def _warn_late_direct_image_operation_conflicts():
    """启动时报告后加载插件造成的裸指令冲突，便于管理员改用稳定入口排查。"""
    for command, operation_name in DIRECT_IMAGE_OPERATION_MAP.items():
        registered_count = sum(
            command in handler.commands
            for handler in CmdHandler.cmd_handlers
        )
        if registered_count > 1:
            logger.warning(
                f"图片操作快捷指令 {command} 与其他插件冲突，"
                f"如无法触发请使用 /img {operation_name}"
            )


# ============================= 其他逻辑 ============================= # 


# 检查图片消息
img_check = CmdHandler(["/img check", '/img_check', '/img info', '/img_info'], logger, priority=1)
img_check.check_cdrate(cd).check_wblist(gbl)
@img_check.handle()
async def _(ctx: HandlerContext):
    data_list = await ctx.aget_image_datas()
    msg = ""
    for i, data in enumerate(data_list):
        with TempFilePath('png') as path:
            msg += f"\n\n【图片{i+1}】"
            try:
                url = data['url']
                await download_file(url, path)
                img = Image.open(path)
                width = img.width
                height = img.height
                msg += f"\n分辨率: {width}x{height}"

                if is_animated(img):
                    msg += f"\n长度: {img.n_frames}帧"
                    if not img.info.get('duration', 0):
                        msg += f"\n帧间隔/FPS: 未知"
                    else:
                        msg += f"\n帧间隔: {img.info['duration']}ms"
                        fps = 1000 / img.info['duration']
                        msg += f"\nFPS: {fps:.2f}"

                if 'file_size' in data:
                    filesize = int(data['file_size'])
                    if not filesize:
                        filesize = os.path.getsize(path)
                    msg += f"\n文件大小: {get_readable_file_size(filesize)}"
                # if 'file' in data:
                #     msg += f"\n文件名: {data['file']}"
                if 'url' in data:
                    msg += f"\n链接: {data['url']}"
                if 'file_unique' in data:
                    msg += f"\n图片标识: {data['file_unique']}"
            except Exception as e:
                logger.print_exc(f"获取 {url} 图片信息失败")
                msg += f"\n无法获取图片信息: {get_exc_desc(e)}"

    return await ctx.asend_fold_msg_adaptive(msg.strip())


# ================================ 即时语录图布局 ================================ #
# 固定横幅只负责业务布局；文字和 Emoji 仍统一交给全局 Painter 绘制。
# 所有固定图层均缓存在进程内，最终图片只即时返回，不写入语录库或图片文件。

SAYING_CANVAS_SIZE = (1200, 400)
SAYING_AVATAR_WIDTH = 400
SAYING_GRADIENT_START = 220
SAYING_GRADIENT_END = 400
SAYING_TEXT_LEFT = 440
SAYING_TEXT_RIGHT = 1150
SAYING_QUOTE_TOP = 52
SAYING_QUOTE_BOTTOM = 302
SAYING_AUTHOR_BOTTOM = 362
SAYING_MAX_FONT_SIZE = 68
SAYING_MIN_FONT_SIZE = 34
SAYING_AUTHOR_FONT_SIZE = 36
SAYING_LINE_SPACING_RATIO = 1.32
SAYING_MAX_INPUT_CHARS = 1024


@lru_cache(maxsize=1)
def _build_saying_gradient_overlay() -> Image.Image:
    """生成并缓存头像到正文之间的固定渐变层。"""

    canvas_width, canvas_height = SAYING_CANVAS_SIZE
    overlay = Image.new("RGBA", SAYING_CANVAS_SIZE, (0, 0, 0, 0))
    pixels = overlay.load()
    span = SAYING_GRADIENT_END - SAYING_GRADIENT_START
    for x in range(canvas_width):
        if x < SAYING_GRADIENT_START:
            alpha = 18
        elif x >= SAYING_GRADIENT_END:
            alpha = 255
        else:
            progress = (x - SAYING_GRADIENT_START) / span
            alpha = round(18 + (255 - 18) * (progress ** 1.7))
        for y in range(canvas_height):
            edge = abs((y / (canvas_height - 1)) - 0.5) * 2
            vignette = round(20 * (edge ** 2))
            pixels[x, y] = (0, 0, 0, min(255, alpha + vignette))
    return overlay


def _crop_saying_avatar(avatar: Image.Image) -> Image.Image:
    """等比裁剪头像填满左侧区域，避免直接缩放导致变形。"""

    target_size = (SAYING_AVATAR_WIDTH, SAYING_CANVAS_SIZE[1])
    source = avatar.convert("RGB")
    scale = max(target_size[0] / source.width, target_size[1] / source.height)
    resized = source.resize(
        (round(source.width * scale), round(source.height * scale)),
        Image.Resampling.LANCZOS,
    )
    left = (resized.width - target_size[0]) // 2
    top = (resized.height - target_size[1]) // 2
    cropped = resized.crop((left, top, left + target_size[0], top + target_size[1]))
    return ImageEnhance.Contrast(cropped).enhance(1.04)


def _get_saying_line_width(units: list[tuple[str, bool]], font) -> float:
    """使用全局 Painter 的 Emoji 测量规则计算一行宽度。"""

    return sum(get_text_width(font, unit_text) for unit_text, _ in units)


def _wrap_saying_units(
    units: list[tuple[str, bool]],
    font,
    max_width: int,
    max_lines: int | None = None,
) -> tuple[list[list[tuple[str, bool]]], bool]:
    """按真实宽度换行，超过可见行数后立即停止并报告溢出。"""

    lines: list[list[tuple[str, bool]]] = [[]]
    current_width = 0.0
    for unit_text, is_emoji in units:
        if not is_emoji and unit_text == "\n":
            if max_lines is not None and len(lines) >= max_lines:
                return lines, True
            lines.append([])
            current_width = 0.0
            continue
        unit_width = get_text_width(font, unit_text)
        if lines[-1] and current_width + unit_width > max_width:
            if max_lines is not None and len(lines) >= max_lines:
                return lines, True
            lines.append([])
            current_width = 0.0
        lines[-1].append((unit_text, is_emoji))
        current_width += unit_width

    for line in lines:
        while line and not line[0][1] and line[0][0] in (" ", "　", "\t"):
            line.pop(0)
    return lines, False


def _layout_saying_quote_at_size(
    units: list[tuple[str, bool]],
    font_size: int,
    max_width: int,
    max_height: int,
) -> tuple[Any, list[list[tuple[str, bool]]], int, bool]:
    """在单一字号下布局正文；只计算最终画布可能显示的行。"""

    font = get_font(DEFAULT_FONT, font_size)
    line_height = round(font_size * SAYING_LINE_SPACING_RATIO)
    max_lines = max(1, max_height // line_height)
    lines, overflow = _wrap_saying_units(
        units,
        font,
        max_width,
        max_lines=max_lines,
    )
    return font, lines, line_height, overflow


def _fit_saying_quote(text: str):
    """选择可读字号；最低字号仍放不下时用省略号截断。"""

    normalized_text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    input_limited = len(normalized_text) > SAYING_MAX_INPUT_CHARS
    if input_limited:
        # 输出区域只有数行，先限制解析量可阻止零宽字符或交替字体制造超大 run 列表。
        normalized_text = normalized_text[:SAYING_MAX_INPUT_CHARS]
    units = list(get_inline_text_layout_units(f"「{normalized_text}」"))
    max_width = SAYING_TEXT_RIGHT - SAYING_TEXT_LEFT
    max_height = SAYING_QUOTE_BOTTOM - SAYING_QUOTE_TOP

    sizes = list(range(SAYING_MIN_FONT_SIZE, SAYING_MAX_FONT_SIZE + 1, 2))
    largest_layout = _layout_saying_quote_at_size(
        units, sizes[-1], max_width, max_height
    )
    if not largest_layout[3] and not input_limited:
        return largest_layout[:3]

    smallest_layout = _layout_saying_quote_at_size(
        units, sizes[0], max_width, max_height
    )
    if not smallest_layout[3] and not input_limited:
        best_layout = smallest_layout
        low, high = 1, len(sizes) - 2
        # 可容纳性随字号单调变化，二分可避免为每个字号重复加载 fallback 字体。
        while low <= high:
            middle = (low + high) // 2
            layout = _layout_saying_quote_at_size(
                units, sizes[middle], max_width, max_height
            )
            if layout[3]:
                high = middle - 1
            else:
                best_layout = layout
                low = middle + 1
        return best_layout[:3]

    font, lines, line_height, _ = smallest_layout
    suffix = list(get_inline_text_units("……」"))
    while lines[-1] and _get_saying_line_width(lines[-1] + suffix, font) > max_width:
        lines[-1].pop()
    lines[-1].extend(suffix)
    return font, lines, line_height


def _fit_saying_author(author: str):
    """在不侵入头像区域的前提下缩小过长署名。"""

    author_text = f"— {author.strip()}"
    max_width = SAYING_TEXT_RIGHT - SAYING_TEXT_LEFT
    low, high = 24, SAYING_AUTHOR_FONT_SIZE
    best_font = get_font(DEFAULT_FONT, low)
    while low <= high:
        middle = (low + high) // 2
        font = get_font(DEFAULT_FONT, middle)
        if get_text_width(font, author_text) <= max_width:
            best_font = font
            low = middle + 1
        else:
            high = middle - 1
    return author_text, best_font


def _render_saying_image(avatar: Image.Image, text: str, author: str) -> Image.Image:
    """在工作线程中生成一张固定尺寸语录图，不产生任何持久化副作用。"""

    canvas = Image.new("RGBA", SAYING_CANVAS_SIZE, BLACK)
    canvas.paste(_crop_saying_avatar(avatar), (0, 0))
    canvas.alpha_composite(_build_saying_gradient_overlay())
    painter = Painter(img=canvas)

    font, lines, line_height = _fit_saying_quote(text)
    quote_area_height = SAYING_QUOTE_BOTTOM - SAYING_QUOTE_TOP
    start_y = SAYING_QUOTE_TOP + max(0, (quote_area_height - len(lines) * line_height) // 2)
    for index, line in enumerate(lines):
        painter._text(
            "".join(unit_text for unit_text, _ in line),
            (SAYING_TEXT_LEFT, start_y + index * line_height),
            font,
            WHITE,
        )

    author_text, author_font = _fit_saying_author(author)
    author_x = max(SAYING_TEXT_LEFT, round(SAYING_TEXT_RIGHT - get_text_width(author_font, author_text)))
    author_y = SAYING_AUTHOR_BOTTOM - round(author_font.size * SAYING_LINE_SPACING_RATIO)
    painter._text(author_text, (author_x, author_y), author_font, (225, 225, 225, 255))
    return painter.img.convert("RGB")


# ================================ 生成语录命令 ================================ #

gen_saying = CmdHandler(['/saying', '/quote', '/语录'], logger)
gen_saying.check_cdrate(cd).check_wblist(gbl).check_group()
@gen_saying.handle()
async def _(ctx: HandlerContext):
    text = None
    try:
        reply_msg = ctx.get_reply_msg()
        reply_cqs = extract_cq_code(reply_msg)
        
        if 'forward' in reply_cqs:
            reply_msg_obj = reply_cqs['forward'][0]['content'][0]
            reply_msg = reply_msg_obj['message']
            reply_user_id = ctx.get_reply_sender().user_id
            reply_user_name = ctx.get_reply_sender().nickname
            text = await extract_special_text(reply_msg, ctx.group_id)
        else:
            reply_user_id = ctx.get_reply_sender().user_id
            reply_user_name = get_user_name_by_event(ctx.event.reply)
            text = await extract_special_text(reply_msg, ctx.group_id)
    except:
        logger.print_exc("生成语录获取回复消息失败")
        raise ReplyException("无法获取回复消息")
    
    if not text:
        raise ReplyException("回复的消息没有文本!")
    
    avatar = await download_image(await get_avatar_url_large(ctx.bot, reply_user_id))
    try:
        image = await run_in_pool(_render_saying_image, avatar, text, reply_user_name)
    finally:
        avatar.close()
    try:
        image_cq = await get_image_cq(image)
    finally:
        image.close()
    return await ctx.asend_reply_msg(image_cq)


# ================================ Markdown转图片 ================================ #

MARKDOWN_COMMAND_MAX_CHARS = 20_000
MARKDOWN_COMMAND_MAX_HEIGHT = 10_000


md = CmdHandler(['/md', '/markdown'], logger)
md.check_cdrate(cd).check_wblist(gbl)
@md.handle()
async def _(ctx: HandlerContext):
    reply_msg = ctx.get_reply_msg()
    assert_and_reply(reply_msg, "请回复一条带有markdown内容的消息")
    text = extract_text(reply_msg).strip()
    assert_and_reply(text, "回复的消息中没有可渲染的Markdown文本")
    assert_and_reply(
        len(text) <= MARKDOWN_COMMAND_MAX_CHARS,
        f"Markdown内容过长，最多支持{MARKDOWN_COMMAND_MAX_CHARS}个字符",
    )
    img = await markdown_to_image(text, max_height=MARKDOWN_COMMAND_MAX_HEIGHT)
    assert_and_reply(img is not None, "Markdown内容过长或格式异常，无法生成图片")
    return await ctx.asend_reply_msg(await get_image_cq(img))



# 色卡
def rgb_to_hsl_values(red: int, green: int, blue: int) -> tuple[int, int, int]:
    """将 0-255 RGB 转为用于用户展示的 HSL 整数值。"""
    hue, lightness, saturation = colorsys.rgb_to_hls(red / 255, green / 255, blue / 255)
    return round(hue * 360), round(saturation * 100), round(lightness * 100)


def color_card(color, additional_text=None):
    if sum(color) > 255 * 3 / 2:
        back_color = BLACK
        front_color = WHITE
    else:
        back_color = WHITE
        front_color = BLACK

    r, g, b = color
    h, s, l = rgb_to_hsl_values(r, g, b)

    text_style = TextStyle(DEFAULT_FONT, 20, front_color)

    with VSplit().set_bg(FillBg(back_color)).set_item_align('c').set_content_align('c').set_padding(8).set_sep(4) as card:
        Spacer(128, 128).set_bg(RoundRectBg((*color, 255), 8))
        if additional_text:
            TextBox(additional_text, text_style)
        TextBox(f"#{r:02x}{g:02x}{b:02x}",  text_style)
        TextBox(f"rgb({r},{g},{b})",        text_style)
        TextBox(f"hsl({h},{s},{l})",        text_style)
    return card

# 颜色显示
color_show = CmdHandler(['/color', '/颜色'], logger)
color_show.check_cdrate(cd).check_wblist(gbl)
@color_show.handle()
async def _(ctx: HandlerContext):
    args = ctx.get_args().strip()

    r, g, b = 0, 0, 0

    try:
        if '#' in args:
            args = args.replace('#', '').strip()
            if len(args) == 3:
                args = ''.join([c*2 for c in args])
            r, g, b = int(args[:2], 16), int(args[2:4], 16), int(args[4:], 16)
        elif 'hsl' in args:
            args = args.replace('hsl', '').strip()
            h, s, l = args.split()
            h = float(h) / 360
            s = float(s.removesuffix('%')) / 100
            l = float(l.removesuffix('%')) / 100
            assert 0 <= h <= 1 and 0 <= s <= 1 and 0 <= l <= 1
            r, g, b = colorsys.hls_to_rgb(h, l, s)
            r, g, b = int(r*255), int(g*255), int(b*255)
        elif 'rgbf' in args:
            args = args.replace('rgbf', '').strip()
            r, g, b = args.split()
            r, g, b = float(r), float(g), float(b)
            r, g, b = int(r*255), int(g*255), int(b*255)
        else:
            args = args.replace('rgb', '').strip()
            r, g, b = args.split()
            r, g, b = int(r), int(g), int(b)
    except:
        logger.print_exc("参数解析失败")
        return await ctx.asend_reply_msg("""
参数错误，使用示例:
/color #aabbcc
/color #abc
/color hsl 120 50 50
/color rgb 255 255 255
/color rgbf 1.0 1.0 1.0
""".strip())
    
    r = max(0, min(255, r))
    g = max(0, min(255, g))
    b = max(0, min(255, b))

    with Canvas(bg=FillBg(WHITE)) as canvas:
        color_card([r, g, b])
    img = await canvas.get_img()
    
    return await ctx.asend_reply_msg(await get_image_cq(img))

# ================================ GIF智能分流 ================================ #
# `/gif` 同时承担静态图片格式转换和视频转 GIF。必须先按回复媒体类型分流，避免
# 同名处理器依赖 NoneBot 优先级争抢命令；显式 `/img gif` 始终只处理图片。
async def _convert_replied_video_to_gif(
    ctx: HandlerContext,
    video: dict,
):
    """解析视频参数、执行受限转换并发送 GIF，输入 video 为回复消息的视频段。"""
    parser = ctx.get_argparser()
    parser.add_argument('--max_size', '-s', type=int, default=config.get('video_to_gif.default_max_size'))
    parser.add_argument('--max_fps', '-f', type=int, default=config.get('video_to_gif.default_max_fps'))
    parser.add_argument('--max_frame_num', '-n', type=int, default=config.get('video_to_gif.default_max_frame_num'))
    args = await parser.parse_args(error_reply="""
    使用方式: (回复一个视频) /gif [--max_size/-s <最大尺寸>] [--max_fps/-f <最大帧率>] [--max_frame_num/-n <最大帧数>]
    --max_size/-s: 图像的长边超过该尺寸时会将视频保持分辨率缩小 默认为256
    --max_fps/-f: 图像的帧率超过该值时会抽帧 默认为10
    --max_frame_num/-n: 图像的帧数量超过该值时会抽帧 默认为200
    示例:  
    (回复一个视频) /gif
    (回复一个视频) /gif -s 512 -f 5 -n 100
    """.strip())
    assert_and_reply(64 <= args.max_size <= 1024, "最大尺寸只能在64-1024之间")
    assert_and_reply(1 <= args.max_fps <= 30, "最大帧率只能在1-30之间")
    assert_and_reply(1 <= args.max_frame_num <= 300, "最大帧数只能在1-300之间")

    filesize = int(video.get('file_size') or 0)
    size_limit = int(config.get('video_to_gif.size_limit') * 1024 * 1024)
    if filesize > 0:
        assert_and_reply(filesize <= size_limit, "视频文件过大，无法处理")

    await ctx.block(f"{ctx.user_id}", 5)
    async with imgtool_job_slot():
        async with TempBotOrInternetFilePath('video', video['file'], ctx.bot) as video_path:
            assert_and_reply(os.path.isfile(video_path), "视频下载失败")
            actual_size = os.path.getsize(video_path)
            assert_and_reply(actual_size <= size_limit, "视频文件过大，无法处理")
            with TempFilePath("gif") as gif_path:
                try:
                    await run_in_pool(
                        convert_video_to_gif,
                        video_path,
                        gif_path,
                        args.max_fps,
                        args.max_size,
                        args.max_frame_num,
                        int(config.get('video_to_gif.probe_timeout_seconds', 15)),
                        int(config.get('video_to_gif.convert_timeout_seconds', 120)),
                        size_limit,
                        pool=IMGTOOL_EXECUTOR,
                    )
                except (ValueError, RuntimeError) as exc:
                    raise ReplyException(str(exc)) from exc
                return await ctx.asend_reply_msg(await get_image_cq(gif_path))


gif_command = CmdHandler(['/gif'], logger)
gif_command.check_cdrate(cd).check_wblist(gbl)


async def _dispatch_gif_command(ctx: HandlerContext):
    """根据直接输入或回复中的媒体类型，将 `/gif` 分派给图片或视频处理流程。"""
    reply_msg = ctx.get_reply_msg() or []
    replied_videos = extract_cq_code(reply_msg).get('video', [])
    image_datas = await ctx.aget_image_datas(
        min_count=None,
        max_count=MULTI_IMAGE_MAX_NUM_CFG.get(),
    )

    assert_and_reply(
        not (image_datas and replied_videos),
        "回复内容同时包含图片和视频，请使用 /img gif 处理图片，或单独回复视频使用 /gif",
    )
    if image_datas:
        return await run_image_operation(ctx, "gif")
    if replied_videos:
        return await _convert_replied_video_to_gif(ctx, replied_videos[0])

    # 裸操作应与 `/img gif` 一致：没有直接输入时仍允许消费当前作用域的图片列表。
    if await get_image_list(ctx):
        return await run_image_operation(ctx, "gif")

    raise ReplyException(
        "请回复静态图片使用 /gif 转换图片格式，或回复视频使用 /gif 转换视频\n"
        "图片操作也可以明确使用：/img gif"
    )


@gif_command.handle()
async def _(ctx: HandlerContext):
    return await _dispatch_gif_command(ctx)
