"""维护预置头像框目录，并按素材参考区域合成带框头像。"""
from dataclasses import dataclass
from pathlib import Path
import json
import math
import re

from PIL import Image, ImageChops, ImageOps


@dataclass(frozen=True)
class CustomAvatarFrame:
    """保存固定编号、素材全名和头像在框画布中的参考区域。"""

    id: int
    name: str
    image: Path
    mask: Path
    avatar_rect: tuple[int, int, int, int]


def load_custom_avatar_catalog(directory: str | Path) -> list[CustomAvatarFrame]:
    """读取每款素材文件夹内的config.json，停用条目保留编号但不供选择。"""
    directory = Path(directory)
    rows = [(path.parent, json.loads(path.read_text(encoding='utf-8')))
            for path in sorted(directory.glob('*/config.json'))]
    frames = []
    ids, names = set(), set()
    for folder, row in rows:
        ident = int(row['id'])
        name = str(row.get('name') or Path(row['image']).stem).strip()
        if ident <= 0 or ident in ids or not name or name in names:
            raise ValueError('头像框编号必须为不重复的正整数，全名必须唯一')
        ids.add(ident)
        names.add(name)
        if not row.get('enabled', True):
            continue
        rect = tuple(int(value) for value in row['avatar_rect'])
        if len(rect) != 4 or min(rect[:2]) < 0 or min(rect[2:]) <= 0:
            raise ValueError(f'头像框 {ident} 的头像参考区域无效')
        frames.append(CustomAvatarFrame(ident, name, folder / row['image'], folder / row['mask'], rect))
    return sorted(frames, key=lambda frame: frame.id)


def resolve_custom_avatar_frame(frames: list[CustomAvatarFrame], selector: str) -> CustomAvatarFrame:
    """按编号或素材全名查找头像框，支持带前导零的编号和PNG扩展名。"""
    selector = selector.strip()
    numeric = int(selector) if selector.isascii() and selector.isdigit() else None
    name = selector[:-4] if selector.lower().endswith('.png') else selector
    for frame in frames:
        if numeric == frame.id or name == frame.name:
            return frame
    raise ValueError(f'找不到头像框“{selector}”，请使用 /头像框列表 查看编号和全名')


def split_custom_avatar_args(args: str) -> tuple[str, str | None]:
    """仅将末尾独立的uN或@QQ识别为账号参数，保留素材全名中的其他文字。"""
    args = args.strip()
    match = re.search(r'(?:^|\s)(u[1-9]\d?|@\d{5,13})$', args)
    return (args[:match.start()].strip(), match[1]) if match else (args, None)


def custom_avatar_selection_key(region: str, uid: str | int) -> str:
    """以被展示账号的区服和UID生成选择键，与查询者QQ无关。"""
    return f'{region}:{uid}'


def render_custom_avatar_border(frame: CustomAvatarFrame, height: int = 100) -> Image.Image:
    """等比缩放透明框本身，列表中间保持透明，不读取头像或蒙版。"""
    with Image.open(frame.image) as source:
        if source.mode != 'RGBA':
            raise ValueError(f'头像框 {frame.id} 必须带透明通道')
        image = source.copy()
    width = max(1, round(image.width * height / image.height))
    return image.resize((width, height), Image.Resampling.LANCZOS)


def render_custom_avatar(avatar: Image.Image, frame: CustomAvatarFrame, avatar_w: int) -> Image.Image:
    """返回保留全部装饰的RGBA图，调用者须按返回图尺寸为布局分配空间。"""
    with Image.open(frame.image) as source:
        if source.mode != 'RGBA':
            raise ValueError(f'头像框 {frame.id} 必须带透明通道')
        border = source.copy()
    with Image.open(frame.mask) as source:
        mask = source.convert('L')
    if mask.size != border.size:
        raise ValueError(f'头像框 {frame.id} 的蒙版尺寸与框图不一致')
    x, y, width, height = frame.avatar_rect
    if x + width > border.width or y + height > border.height or avatar_w <= 0:
        raise ValueError(f'头像框 {frame.id} 的头像参考区域越界')
    portrait = ImageOps.fit(avatar.convert('RGBA'), (width, height), method=Image.Resampling.LANCZOS)
    layer = Image.new('RGBA', border.size)
    layer.paste(portrait, (x, y))
    # 同时保留原头像透明度和开口蒙版，参考区域外不会出现不透明黑色。
    layer.putalpha(ImageChops.multiply(layer.getchannel('A'), mask))
    merged = Image.alpha_composite(layer, border)
    factor = avatar_w / width
    size = tuple(math.ceil(value * factor) for value in merged.size)
    return merged.resize(size, Image.Resampling.LANCZOS)
