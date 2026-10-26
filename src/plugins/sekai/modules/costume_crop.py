"""识别衣装图片类型后裁切，模型失败时使用固定框。"""
from collections import OrderedDict
from dataclasses import dataclass
from functools import lru_cache
import json
import logging
import os
from pathlib import Path
from threading import RLock

from PIL import Image, ImageChops, ImageDraw, ImageStat


# ================================ 已确认的固定紧框 ================================ #
def fixed_bounds(size, layout='paired'):
    """按构图选择固定紧框，日服双图保留已确认的上移与顶部内收。"""
    width, height = size
    if layout == 'wide':
        # 国服单图的男女款横向同框，使用中部多人区域，不能套左侧日服试穿窗。
        return [round(value * extent) for value, extent in zip((.22, .075, .77, .81), (width, height) * 2)]
    viewport = (.18 * width, .13 * height, .59 * width, .69 * height)
    inset_x = (viewport[2] - viewport[0]) * .08
    inset_y = (viewport[3] - viewport[1]) * .08
    left, top, right, bottom = [round(value) for value in (
        viewport[0] + inset_x, viewport[1] + inset_y,
        viewport[2] - inset_x, viewport[3] - inset_y)]
    shift = round(-height / 120)
    top += shift + round(height / 240)
    bottom += shift
    return [left, top, right, bottom]


# ================================ 双图真实用途辨别 ================================ #
@dataclass(frozen=True)
class CostumeClassification:
    """三态分类；unknown既不是插画，也不能直接套用试穿裁框。"""
    role: str
    reason: str
    outer_match: float | None = None
    outer_mean: float | None = None


def classify_costume_pair(first, second, has_numbered_titles=False):
    """比较官方主体窗外的共享背景；缺标题仍可识别，证据落在灰区则保持未知。"""
    if first is None or second is None:
        return CostumeClassification('unknown', 'missing_pair_image')
    if min(*first.size, *second.size) < 32:
        return CostumeClassification('unknown', 'image_too_small')
    first_ratio = first.width / first.height
    second_ratio = second.width / second.height
    if abs(first_ratio / second_ratio - 1) > .01:
        return CostumeClassification('unknown', 'incompatible_aspect_ratio')

    # 只对252×144的分析副本比较，不改原图；允许CDN和本地有不同分辨率。
    size = (252, 144)
    first_small = first.convert('RGB').resize(size, Image.Resampling.BICUBIC)
    second_small = second.convert('RGB').resize(size, Image.Resampling.BICUBIC)
    if min(max(ImageStat.Stat(image).stddev) for image in (first_small, second_small)) < 5:
        return CostumeClassification('unknown', 'insufficient_visual_information')
    difference = ImageChops.difference(first_small, second_small)
    red, green, blue = difference.split()
    maximum = ImageChops.lighter(ImageChops.lighter(red, green), blue)
    outer_mask = Image.new('L', size, 255)
    # 官方试穿主体窗为宽18%～59%、高13%～69%；外侧比只看右边更能排除630等插画。
    ImageDraw.Draw(outer_mask).rectangle((45, 19, 148, 98), fill=0)
    histogram = maximum.histogram(mask=outer_mask)
    count = sum(histogram)
    outer_match = sum(histogram[:12]) / count
    outer_mean = sum(value * quantity for value, quantity in enumerate(histogram)) / count
    inner_histogram = maximum.histogram(mask=ImageChops.invert(outer_mask))
    inner_changed = sum(inner_histogram[12:]) / sum(inner_histogram)

    # 不同人物且外背景高度一致才判为双试穿；相同图片误传、低信息图片不可作为证据。
    if outer_match >= .98 and outer_mean <= 2 and inner_changed >= .03:
        reason = 'paired_titles_shared_surroundings' if has_numbered_titles else 'shared_surroundings_without_titles'
        return CostumeClassification('dual_lineup', reason, outer_match, outer_mean)
    # 这两个阈值间留出灰区；不把“未满足双试穿”直接当成“确定是插画”。
    if outer_match <= .90 and outer_mean >= 8:
        return CostumeClassification('illustration_and_lineup', 'different_composition', outer_match, outer_mean)
    return CostumeClassification('unknown', 'ambiguous_composition', outer_match, outer_mean)


# ================================ 同素材版本分类缓存 ================================ #
_ROLE_CACHE = OrderedDict()
_ROLE_CACHE_LIMIT = 64


# ================================ 图1用途持久缓存 ================================ #
_FIRST_ROLE_LIMIT = 256
_FIRST_ROLE_SCHEMA = 1
_ROLE_LOCK = RLock()
_role_logger = logging.getLogger(__name__)


class _FirstImageRoles:
    """保存已确认的图1用途，素材版本变化时使用独立记录。"""

    def __init__(self, path):
        self.path = Path(path)
        self.entries = OrderedDict()
        try:
            payload = json.loads(self.path.read_text(encoding='utf-8'))
            if payload.get('version') == _FIRST_ROLE_SCHEMA:
                self.entries.update(
                    (key, role) for key, role in payload['entries'].items()
                    if isinstance(key, str) and role in ('illustration', 'lineup'))
                while len(self.entries) > _FIRST_ROLE_LIMIT:
                    self.entries.popitem(last=False)
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            _role_logger.warning('衣装用途缓存读取失败，将按实际素材重新判断', exc_info=True)

    def get(self, key):
        """返回同版本图1已确认的用途，并更新内存访问顺序。"""
        value = self.entries.get(key)
        if value is not None:
            self.entries.move_to_end(key)
        return value

    def remember(self, key, role):
        """登记确定用途；写盘失败时仍保留本次可用结果。"""
        if self.entries.get(key) == role:
            self.entries.move_to_end(key)
            return
        self.entries[key] = role
        self.entries.move_to_end(key)
        while len(self.entries) > _FIRST_ROLE_LIMIT:
            self.entries.popitem(last=False)
        temporary = self.path.with_name(self.path.name + '.tmp')
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(
                {'version': _FIRST_ROLE_SCHEMA, 'entries': self.entries},
                ensure_ascii=False), encoding='utf-8')
            os.replace(temporary, self.path)
        except OSError:
            _role_logger.warning('衣装用途缓存保存失败，当前进程继续使用已确认结果', exc_info=True)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


@lru_cache(maxsize=4)
def _first_image_roles(path):
    """复用同一路径的用途缓存；调用方在分类锁内访问。"""
    return _FirstImageRoles(path)


def resolve_costume_pair(first, second, has_numbered_titles=False, cache_key=None,
                         *, first_image_key=None, role_cache_path=None):
    """识别双图用途；图2缺失时可复用同版本图1的持久记录。"""
    # 分类、内存缓存和文件替换共用锁，由异步调用方放在线程中执行。
    with _ROLE_LOCK:
        store = None
        first_key = None
        if first is not None and first_image_key is not None and role_cache_path is not None:
            store = _first_image_roles(str(Path(role_cache_path).resolve()))
            first_key = json.dumps(
                [_FIRST_ROLE_SCHEMA, first_image_key], ensure_ascii=False, separators=(',', ':'))

        if first is not None and second is None and store is not None:
            role = store.get(first_key)
            if role is not None:
                pair_role = 'dual_lineup' if role == 'lineup' else 'illustration_and_lineup'
                return CostumeClassification(pair_role, 'confirmed_first_image_version')

        if cache_key is not None and cache_key in _ROLE_CACHE:
            result = _ROLE_CACHE.pop(cache_key)
            _ROLE_CACHE[cache_key] = result
        else:
            result = classify_costume_pair(first, second, has_numbered_titles)
            if cache_key is not None and result.role != 'unknown':
                _ROLE_CACHE[cache_key] = result
                while len(_ROLE_CACHE) > _ROLE_CACHE_LIMIT:
                    _ROLE_CACHE.popitem(last=False)

        # 只有两张实际图片共同确认的结果才能登记，未知和单图复用均不造新证据。
        if first is not None and second is not None and store is not None and result.role != 'unknown':
            role = 'lineup' if result.role == 'dual_lineup' else 'illustration'
            store.remember(first_key, role)
        return result


def first_image_is_lineup(first, second, has_numbered_titles):
    """兼容原有打样入口；未知返回None，不能再默认认作插画。"""
    role = classify_costume_pair(first, second, has_numbered_titles).role
    return None if role == 'unknown' else role == 'dual_lineup'


# ================================ 模型比较沿用的预处理与边界 ================================ #
def predict_direct_mask(session, image):
    """使用模型原先的等比补黑输入；不套用rembg的拉伸、减均值和输出归一化。"""
    import cv2
    import numpy as np
    pixels = np.asarray(image.convert('RGB'), dtype=np.float32) / 255
    height, width = pixels.shape[:2]
    side = 1024
    scaled_h, scaled_w = (side, int(side * width / height)) if height > width else (int(side * height / width), side)
    top, left = (side - scaled_h) // 2, (side - scaled_w) // 2
    tensor = np.zeros((side, side, 3), dtype=np.float32)
    tensor[top:top + scaled_h, left:left + scaled_w] = cv2.resize(pixels, (scaled_w, scaled_h))
    tensor = np.transpose(tensor, (2, 0, 1))[None]
    mask = session.run(None, {session.get_inputs()[0].name: tensor})[0][0, 0]
    return cv2.resize(mask[top:top + scaled_h, left:left + scaled_w], (width, height))


def direct_bounds(probabilities, size, layout='paired'):
    """按构图提取主体密集区域，日服试穿窗与国服多人图分别约束。"""
    import math
    import cv2
    import numpy as np
    if layout == 'wide':
        return wide_lineup_bounds(probabilities, size)
    width, height = size
    working_width = 630
    working_height = round(height * working_width / width)
    binary = cv2.resize((probabilities >= .9).astype(np.uint8), (working_width, working_height), interpolation=cv2.INTER_NEAREST).astype(bool)
    left, top, right, bottom = [round(value * extent) for value, extent in zip((.18, .13, .59, .69), (working_width, working_height) * 2)]
    allowed = np.zeros_like(binary)
    allowed[top:bottom, left:right] = binary[top:bottom, left:right]
    body_top, body_bottom = round(.28 * working_height), round(.58 * working_height)
    columns = np.flatnonzero(allowed[body_top:body_bottom].sum(axis=0) >= .2 * (body_bottom - body_top))
    if not len(columns):
        raise ValueError('没有主体密集列，不能扩大为原图')
    chosen_left, chosen_right = int(columns[0]), int(columns[-1]) + 1
    rows = np.flatnonzero(allowed[:, chosen_left:chosen_right].sum(axis=1) >= .28 * (chosen_right - chosen_left))
    if not len(rows):
        raise ValueError('没有主体密集行，不能扩大为原图')
    scale = width / working_width
    padding = (chosen_right - chosen_left) * scale / 536
    candidate = np.array([max(0, math.floor(chosen_left * scale - padding)),
                          max(0, math.floor(int(rows[0]) * scale - padding)),
                          min(width, math.ceil(chosen_right * scale + padding)),
                          min(height, math.ceil((int(rows[-1]) + 1) * scale + padding))], dtype=float)
    fixed = fixed_bounds(size)
    fixed[1] -= round(height / 240)
    frame_height = fixed[3] - fixed[1]
    candidate += [0, -frame_height * .03, 0, frame_height * .03]
    lower = [0, fixed[1] - frame_height * .04, 0, fixed[3] - frame_height * .02]
    upper = [width, fixed[1] + frame_height * .02, width, fixed[3] + frame_height * .04]
    return np.rint(np.clip(candidate, lower, upper)).astype(int).tolist()


def wide_lineup_bounds(probabilities, size):
    """在国服多人同框区域提取密集主体，裁框限制在中部以减少残留背景。"""
    import cv2
    import numpy as np
    width, height = size
    working_width = 630
    working_height = round(height * working_width / width)
    binary = cv2.resize((probabilities >= .9).astype(np.uint8),
                        (working_width, working_height), interpolation=cv2.INTER_NEAREST).astype(bool)
    left, top, right, bottom = [round(value * extent) for value, extent in
                               zip((.14, .04, .84, .87), (working_width, working_height) * 2)]
    allowed = np.zeros_like(binary)
    allowed[top:bottom, left:right] = binary[top:bottom, left:right]
    body_top, body_bottom = round(.3 * working_height), round(.65 * working_height)
    columns = np.flatnonzero(allowed[body_top:body_bottom].sum(axis=0) >= .3 * (body_bottom - body_top))
    if not len(columns):
        raise ValueError('国服衣装图没有密集主体列')
    chosen_left, chosen_right = int(columns[0]), int(columns[-1]) + 1
    rows = np.flatnonzero(allowed[:, chosen_left:chosen_right].sum(axis=1) >= .38 * (chosen_right - chosen_left))
    if not len(rows):
        raise ValueError('国服衣装图没有密集主体行')
    scale = width / working_width
    # 身体密集行用于下边界；头发和帽子在顶部较稀疏，另从有效前景连通块保留顶部。
    count, labels, stats, _ = cv2.connectedComponentsWithStats(allowed.astype(np.uint8), 8)
    minimum_area = max(12, round(working_width * working_height * .0005))
    valid_labels = [index for index in range(1, count) if stats[index, cv2.CC_STAT_AREA] >= minimum_area
                    and stats[index, cv2.CC_STAT_HEIGHT] >= round(.04 * working_height)]
    significant = np.isin(labels, valid_labels)
    head_rows = np.flatnonzero(significant[:body_top, chosen_left:chosen_right].sum(axis=1)
                              >= max(3, round(.012 * (chosen_right - chosen_left))))
    head_top = int(head_rows[0]) if len(head_rows) else int(rows[0])
    chosen_top = max(round(.04 * height), round(head_top * scale - .01 * height))
    chosen_bottom = min(round(.83 * height), round((int(rows[-1]) + 1) * scale + .02 * height))
    return [round(chosen_left * scale), chosen_top, round(chosen_right * scale), chosen_bottom]
