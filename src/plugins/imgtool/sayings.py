"""语录的生成绘图、持久收录、分页检索与群内命令。"""

from ..utils import *
from PIL import Image, ImageDraw, ImageEnhance, ImageOps
from functools import lru_cache
import io

import asyncio
from dataclasses import dataclass
from pathlib import Path
import re
import time
import json
import hashlib
import math
from collections import Counter
from bisect import bisect_right

import aiosqlite
import aiohttp


# ================================ 查询参数 ================================ #

def parse_saying_query(args: str, mentioned_ids: list[int], default_id: int) -> tuple[int | None, int, str]:
    """解析作者、页码和配色；默认查询全群，全部或 all 返回空作者。"""
    author_ids = list(dict.fromkeys(mentioned_ids))
    tokens = args.strip().split()
    themes = {"浅色": "light", "深色": "dark"}
    selected_themes = [token for token in tokens if token in themes]
    if len(selected_themes) > 1:
        raise ValueError("请选择一种背景：浅色或深色，例如 /查语录 @群友 深色")
    theme = themes[selected_themes[0]] if selected_themes else "light"
    tokens = [token for token in tokens if token not in themes]
    page = 1
    # p2/page2 明确表示页码，纯数字仍用于指定 QQ；兼容已经使用的中文页码。
    if tokens and (match := re.fullmatch(
            r"(?:p(?:age)?([1-9]\d{0,8})|第?([1-9]\d{0,8})页)", tokens[-1], re.IGNORECASE)):
        page = int(match.group(1) or match.group(2))
        tokens.pop()
    if any(token.lower() in {"全部", "all"} for token in tokens):
        if len(tokens) != 1 or author_ids:
            raise ValueError("查询全部语录时请不要同时指定群友，例如 /查语录 all p2")
        return None, page, theme
    if len(author_ids) > 1 or len(tokens) > 1:
        raise ValueError("请指定一位群友，例如 /查语录 @群友 p2")
    if tokens:
        if author_ids or (tokens[0] != "自己" and not re.fullmatch(r"[1-9]\d{4,18}", tokens[0])):
            raise ValueError("请使用 /查语录 all、/查语录 自己、/查语录 @群友 或 /查语录 QQ号，可附加 p2、page2、浅色或深色")
        author_ids.append(default_id if tokens[0] == "自己" else int(tokens[0]))
    if not author_ids:
        return None, page, theme
    author_id = author_ids[0]
    if not 0 < author_id < 2 ** 63:
        raise ValueError("QQ号无效，请指定一位群友")
    return author_id, page, theme


def parse_saying_number(args: str) -> int:
    """解析正整数编号或 #编号。"""
    match = re.fullmatch(r"#?([1-9]\d{0,8})", args.strip())
    if not match:
        raise ValueError("请填写本群语录编号，例如 /语录 12")
    return int(match.group(1))


# ================================ 持久化数据 ================================ #

@dataclass(frozen=True)
class Saying:
    group_id: int
    number: int
    author_id: int
    nickname: str
    text: str
    source_msg_id: int
    recorder_id: int
    created_at: int
    image_size: int
    content_key: str = ""
    extension: str = "png"
    media_json: str = "[]"


class SayingLimitError(Exception):
    """表示收录容量达到上限，即时出图仍可继续。"""


class SayingPageError(Exception):
    pass


class SayingStore:
    """串行处理索引和文件操作；图片仅保存在当前实例对应的数据目录。"""

    def __init__(self, db_path: str | Path, image_dir: str | Path):
        self.db_path = Path(db_path)
        self.image_dir = Path(image_dir)
        self._connection: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def _connect(self) -> aiosqlite.Connection:
        """在实例锁内按需建立表和索引。"""
        if self._connection is None:
            await asyncio.to_thread(self.db_path.parent.mkdir, parents=True, exist_ok=True)
            connection = await aiosqlite.connect(self.db_path)
            connection.row_factory = aiosqlite.Row
            try:
                await connection.executescript("""
                    CREATE TABLE IF NOT EXISTS sayings (
                        group_id INTEGER NOT NULL,
                        number INTEGER NOT NULL,
                        author_id INTEGER NOT NULL,
                        nickname TEXT NOT NULL,
                        text TEXT NOT NULL,
                        source_msg_id INTEGER NOT NULL,
                        recorder_id INTEGER NOT NULL,
                        created_at INTEGER NOT NULL,
                        image_size INTEGER NOT NULL,
                        PRIMARY KEY (group_id, number),
                        UNIQUE (group_id, source_msg_id)
                    );
                    CREATE INDEX IF NOT EXISTS sayings_author
                        ON sayings (group_id, author_id, number DESC);
                    CREATE INDEX IF NOT EXISTS sayings_content
                        ON sayings (group_id, author_id, text, number);
                    CREATE TABLE IF NOT EXISTS saying_counters (
                        group_id INTEGER PRIMARY KEY,
                        last_number INTEGER NOT NULL
                    );
                """)
                # 旧的纯文字索引原位升级；旧 PNG 文件和编号继续使用。
                async with connection.execute("PRAGMA table_info(sayings)") as cursor:
                    columns = {row["name"] for row in await cursor.fetchall()}
                for name, default in (("content_key", "''"), ("extension", "'png'"),
                                      ("media_json", "'[]'")):
                    if name not in columns:
                        await connection.execute(
                            f"ALTER TABLE sayings ADD COLUMN {name} TEXT NOT NULL DEFAULT {default}")
                await connection.execute("""
                    CREATE INDEX IF NOT EXISTS sayings_media_content
                    ON sayings (group_id, author_id, content_key, number)
                """)
                await connection.commit()
            except BaseException:
                await connection.close()
                raise
            self._connection = connection
        return self._connection

    def image_path(self, saying: Saying) -> Path:
        """根据数据库中的群号和编号确定图片路径。"""
        extension = saying.extension if saying.extension in ("png", "gif") else "png"
        return self.image_dir / str(saying.group_id) / f"{saying.number}.{extension}"

    def media_paths(self, saying: Saying) -> list[Path]:
        """只按受控编号生成素材路径，数据库中的外部 URL 不作为文件路径。"""
        return [self.image_dir / str(saying.group_id) / f"{saying.number}-media" /
                f"{index}.{item['extension']}"
                for index, item in enumerate(json.loads(saying.media_json))
                if item['extension'] in ("png", "gif", "jpg", "webp")]

    async def read_media(self, saying: Saying) -> list[bytes]:
        """读取当前群语录关联的原始素材，保留动图与长截图。"""
        async with self._lock:
            return [await asyncio.to_thread(path.read_bytes) for path in self.media_paths(saying)]

    async def _fetch_one(self, connection, query, parameters):
        async with connection.execute(query, parameters) as cursor:
            return await cursor.fetchone()

    async def _read_image(self, saying: Saying) -> bytes | None:
        try:
            return await asyncio.to_thread(self.image_path(saying).read_bytes)
        except FileNotFoundError:
            return None

    async def find_source(self, group_id: int, source_msg_id: int) -> tuple[Saying, bytes | None] | None:
        """生成前检查同群原消息，已有记录直接复用原图。"""
        async with self._lock:
            connection = await self._connect()
            row = await self._fetch_one(connection,
                "SELECT * FROM sayings WHERE group_id = ? AND source_msg_id = ?",
                (group_id, source_msg_id))
            if row is None:
                return None
            saying = Saying(**dict(row))
            return saying, await self._read_image(saying)

    async def find_content(self, group_id: int, author_id: int, text: str,
                           content_key: str = "") -> tuple[Saying, bytes | None] | None:
        """按同群作者与完整正文查重，复用最早收录的编号和原图。"""
        async with self._lock:
            connection = await self._connect()
            if content_key:
                query = "SELECT * FROM sayings WHERE group_id = ? AND author_id = ? AND content_key = ? ORDER BY number LIMIT 1"
                parameters = (group_id, author_id, content_key)
            else:
                query = "SELECT * FROM sayings WHERE group_id = ? AND author_id = ? AND text = ? AND content_key = '' ORDER BY number LIMIT 1"
                parameters = (group_id, author_id, text)
            row = await self._fetch_one(connection, query, parameters)
            if row is None:
                return None
            saying = Saying(**dict(row))
            return saying, await self._read_image(saying)

    async def get(self, group_id: int, number: int) -> tuple[Saying, bytes | None] | None:
        """按群与编号读取成品图快照，发送过程使用独立的图片字节。"""
        async with self._lock:
            connection = await self._connect()
            row = await self._fetch_one(connection,
                "SELECT * FROM sayings WHERE group_id = ? AND number = ?", (group_id, number))
            if row is None:
                return None
            saying = Saying(**dict(row))
            return saying, await self._read_image(saying)

    async def save(self, *, group_id: int, author_id: int, nickname: str, text: str,
                   source_msg_id: int, recorder_id: int, png: bytes,
                   max_per_group: int, max_total_bytes: int, content_key: str = "",
                   extension: str = "png", media: tuple = ()) -> tuple[Saying, bool]:
        """保存成品图及索引，同群同作者的相同正文复用已有编号。"""
        async with self._lock:
            connection = await self._connect()
            image_path = None
            written_paths = []
            await connection.execute("BEGIN IMMEDIATE")
            try:
                row = await self._fetch_one(connection,
                    "SELECT * FROM sayings WHERE group_id = ? AND source_msg_id = ?",
                    (group_id, source_msg_id))
                # 写事务内再次查正文，避免不同消息并发收录时重复分配编号。
                if row is None:
                    query_field = "content_key" if content_key else "text"
                    extra = "" if content_key else " AND content_key = ''"
                    row = await self._fetch_one(connection,
                        f"SELECT * FROM sayings WHERE group_id = ? AND author_id = ? AND {query_field} = ?{extra} ORDER BY number LIMIT 1",
                        (group_id, author_id, content_key or text))
                if row is not None:
                    await connection.rollback()
                    return Saying(**dict(row)), False

                # 容量按现存记录统计，旧语录由用户管理，达到上限后仍可即时出图。
                row = await self._fetch_one(connection,
                    "SELECT COUNT(*) AS count FROM sayings WHERE group_id = ?", (group_id,))
                if row["count"] >= max_per_group:
                    raise SayingLimitError("本群语录数量已达上限")
                row = await self._fetch_one(connection,
                    "SELECT COALESCE(SUM(image_size), 0) AS size FROM sayings", ())
                total_size = len(png) + sum(len(item[1]) for item in media)
                if row["size"] + total_size > max_total_bytes:
                    raise SayingLimitError("语录图片存储空间已达上限")

                row = await self._fetch_one(connection,
                    "SELECT last_number FROM saying_counters WHERE group_id = ?", (group_id,))
                number = row["last_number"] + 1 if row is not None else 1
                if extension not in ("png", "gif"):
                    raise ValueError("语录成品格式无效")
                metadata = [{"extension": item[0]} for item in media]
                if any(item["extension"] not in ("png", "gif", "jpg", "webp") for item in metadata):
                    raise ValueError("语录素材格式无效")
                saying = Saying(group_id, number, author_id, nickname, text,
                                source_msg_id, recorder_id, int(time.time()), total_size,
                                content_key, extension, json.dumps(metadata))
                image_path = self.image_path(saying)
                written_paths = [image_path, *self.media_paths(saying)]

                # 原图写入完成后提交索引。请求取消时等待文件线程结束，随后清理未提交图片。
                def write_image():
                    for path, data in zip(written_paths, [png, *(item[1] for item in media)]):
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(data)

                write_task = asyncio.create_task(asyncio.to_thread(write_image))
                try:
                    await asyncio.shield(write_task)
                except asyncio.CancelledError:
                    await write_task
                    raise
                await connection.execute("""INSERT INTO sayings
                    (group_id, number, author_id, nickname, text, source_msg_id, recorder_id,
                     created_at, image_size, content_key, extension, media_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (group_id, number, author_id, nickname, text, source_msg_id,
                     recorder_id, saying.created_at, total_size, content_key, extension, saying.media_json))
                await connection.execute("""
                    INSERT INTO saying_counters VALUES (?, ?)
                    ON CONFLICT(group_id) DO UPDATE SET last_number = excluded.last_number
                """, (group_id, number))
                await connection.commit()
                return saying, True
            except BaseException:
                await connection.rollback()
                await asyncio.to_thread(self._remove_files, written_paths)
                raise

    @staticmethod
    def _remove_files(paths):
        """清理指定快照及其空素材目录，继续尝试删除其他文件后报告首个失败。"""
        first_error = None
        for path in paths:
            try:
                path.unlink(missing_ok=True)
                if path.parent.name.endswith("-media") and path.parent.exists():
                    if not any(path.parent.iterdir()):
                        path.parent.rmdir()
            except OSError as error:
                first_error = first_error or error
        if first_error:
            raise first_error

    async def list_page(self, group_id: int, author_id: int | None, page: int,
                        page_size: int) -> tuple[list[tuple[Saying, bytes | None]], int, int]:
        """分页读取本群或指定作者的原图，通过页大小约束文件读取量。"""
        async with self._lock:
            connection = await self._connect()
            # 全群和个人查询共用分页，群号始终是必选条件；主键支持本群编号倒序。
            where_clause = "group_id = ?"
            parameters = (group_id,)
            if author_id is not None:
                where_clause += " AND author_id = ?"
                parameters += (author_id,)
            row = await self._fetch_one(connection,
                f"SELECT COUNT(*) AS count FROM sayings WHERE {where_clause}", parameters)
            total = row["count"]
            page_count = (total + page_size - 1) // page_size
            if total == 0:
                return [], 0, 0
            if page < 1 or page > page_count:
                raise SayingPageError(f"共 {page_count} 页，请输入有效页码")
            async with connection.execute(f"""
                SELECT * FROM sayings WHERE {where_clause}
                ORDER BY number DESC LIMIT ? OFFSET ?
            """, parameters + (page_size, (page - 1) * page_size)) as cursor:
                sayings = [Saying(**dict(row)) for row in await cursor.fetchall()]
            images = [(saying, await self._read_image(saying)) for saying in sayings]
            return images, total, page_count

    async def delete(self, group_id: int, number: int, actor_id: int,
                     is_admin: bool) -> tuple[Saying, OSError | None] | None:
        """作者、收录人及本群管理员可删除；先撤下索引，再清理对应图片。"""
        async with self._lock:
            connection = await self._connect()
            row = await self._fetch_one(connection,
                "SELECT * FROM sayings WHERE group_id = ? AND number = ?", (group_id, number))
            if row is None:
                return None
            saying = Saying(**dict(row))
            if actor_id not in (saying.author_id, saying.recorder_id) and not is_admin:
                raise PermissionError("仅原作者、收录人或群管理员可以删除这条语录")
            try:
                await connection.execute("DELETE FROM sayings WHERE group_id = ? AND number = ?",
                                         (group_id, number))
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise
            cleanup_error = None
            try:
                await asyncio.to_thread(self._remove_files,
                                        [self.image_path(saying), *self.media_paths(saying)])
            except OSError as error:
                cleanup_error = error
            return saying, cleanup_error

    async def close(self):
        """Bot 正常关闭时释放 SQLite 连接和工作线程。"""
        async with self._lock:
            if self._connection is not None:
                await self._connection.close()
                self._connection = None


# ================================ 即时语录图布局 ================================ #
# 固定横幅只负责业务布局；文字和 Emoji 仍统一交给全局 Painter 绘制。
# 固定图层使用进程缓存，命令层负责成品图的发送与保存。

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
SAYING_ALBUM_COLUMNS = 2
# 600px 宽缩略图的最小正文约 17px，兼顾每页 16 张的体积与可读性。
SAYING_ALBUM_IMAGE_SIZE = (600, 200)
SAYING_ALBUM_MAX_PAGE_SIZE = 16
SAYING_ALBUM_JPEG_QUALITY = 90
SAYING_ALBUM_THEMES = {
    "light": {
        "background": (226, 222, 216), "card": (245, 243, 239),
        "border": (197, 192, 184), "title": (56, 53, 49),
        "secondary": (117, 111, 103), "number": (94, 87, 77),
    },
    "dark": {
        "background": (54, 54, 57), "card": (39, 39, 42),
        "border": (78, 78, 82), "title": (237, 235, 231),
        "secondary": (172, 172, 177), "number": (211, 208, 201),
    },
}


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
    """按原比例缩放头像，并居中裁剪到左侧区域。"""

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
    """选择可读字号，超出最小字号容量的文本以省略号收尾。"""

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
        # 可容纳性随字号单调变化，二分查找可减少字号和 fallback 字体的试算次数。
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
    """将过长署名缩小到头像右侧的可用宽度。"""

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
    """在工作线程中生成固定尺寸语录图，供即时发送和持久保存。"""

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


# ================================ 有序消息与素材读取 ================================ #

SAYING_MEDIA_MAX_COUNT = 8
SAYING_MEDIA_MAX_BYTES = 20 * 1024 * 1024
SAYING_MEDIA_TOTAL_BYTES = 32 * 1024 * 1024
SAYING_MEDIA_MAX_PIXELS = 16 * 1024 * 1024
SAYING_GIF_MAX_FRAMES = 120
SAYING_GIF_MAX_DURATION = 8000
SAYING_GIF_MAX_PIXELS = 24 * 1024 * 1024
SAYING_OUTPUT_MAX_BYTES = 20 * 1024 * 1024
_saying_render_slots = asyncio.Semaphore(2)
_saying_face_lock = asyncio.Lock()
_saying_face_urls = {}
_saying_face_cache = {}


@dataclass
class SayingMedia:
    """保留原始文件及工作线程解码结果，首帧用于排版，缩小帧用于动画合成。"""
    data: bytes
    extension: str
    first: Image.Image
    frames: list
    durations: list
    loop: int | None
    fallback: str = ""

    def close(self):
        self.first.close()
        for frame in self.frames:
            frame.close()


async def _download_saying_bytes(url: str, limit: int) -> bytes:
    """限时分块下载 QQ 素材；在进入 Pillow 前约束文件体积。"""
    if not isinstance(url, str) or not url.startswith(("https://", "http://")):
        raise ValueError("图片没有可下载的网络地址")
    async with get_client_session().get(url, timeout=aiohttp.ClientTimeout(total=15)) as response:
        response.raise_for_status()
        if response.content_length is not None and response.content_length > limit:
            raise ValueError("图片文件过大")
        chunks = bytearray()
        async for chunk in response.content.iter_chunked(65536):
            chunks.extend(chunk)
            if len(chunks) > limit:
                raise ValueError("图片文件过大")
        return bytes(chunks)


async def _download_saying_face(face_id: str) -> bytes:
    """按 QQNT 索引获取真实表情；锁合并首次请求，内存缓存限定 64 项和 8MB。"""
    if not face_id.isdigit():
        raise ValueError("表情编号无效")
    async with _saying_face_lock:
        cached = _saying_face_cache.pop(face_id, None)
        if cached is not None:
            _saying_face_cache[face_id] = cached
            return cached
        if not _saying_face_urls:
            raw = await _download_saying_bytes(
                "https://koishi.js.org/QFace/assets/qq_emoji/_index.json", 2 * 1024 * 1024)
            catalog = await run_in_pool(json.loads, raw)
            for item in catalog[:2048]:
                identifier = str(item.get("emojiId", ""))
                asset = next((entry for entry in item.get("assets", [])
                              if entry.get("type") == 0 and entry.get("name") == f"{identifier}.png"), None)
                if asset and asset.get("path", "").startswith("assets/") and ".." not in asset["path"]:
                    _saying_face_urls[identifier] = "https://koishi.js.org/QFace/" + asset["path"]
        url = _saying_face_urls.get(face_id)
        if not url:
            raise ValueError("暂无这个 QQ 表情的素材")
        data = await _download_saying_bytes(url, 2 * 1024 * 1024)
        _saying_face_cache[face_id] = data
        while len(_saying_face_cache) > 64 or sum(map(len, _saying_face_cache.values())) > 8 * 1024 * 1024:
            _saying_face_cache.pop(next(iter(_saying_face_cache)))
        return data


async def _download_saying_media(bot, segment) -> bytes:
    """先使用消息地址；QQ 地址失效时向 OneBot 刷新一次，不做持续重试。"""
    if segment["type"] == "face":
        return await _download_saying_face(str(segment["data"].get("id", "")))
    fields = segment["data"]
    url = fields.get("url") or fields.get("file", "")
    try:
        return await _download_saying_bytes(url, SAYING_MEDIA_MAX_BYTES)
    except (aiohttp.ClientError, TimeoutError, ValueError):
        if not fields.get("file"):
            raise
    refreshed = await bot.call_api("get_image", file=fields["file"])
    url = refreshed.get("url") or refreshed.get("file", "")
    # 不把消息提供的本地路径当作 Bot 进程文件读取，两个容器可能也不共享路径。
    return await _download_saying_bytes(url, SAYING_MEDIA_MAX_BYTES)


def _decode_saying_media(data: bytes, frame_budget: int) -> SayingMedia:
    """解码完整逻辑画布并约束动态图内存；超限保留首帧与原始文件。"""
    frames = []
    durations = []
    first = None
    try:
        with Image.open(io.BytesIO(data)) as source:
            if source.width * source.height > SAYING_MEDIA_MAX_PIXELS:
                raise ValueError("图片分辨率过大")
            extension = {"PNG": "png", "JPEG": "jpg", "GIF": "gif", "WEBP": "webp"}.get(source.format)
            if extension is None:
                raise ValueError("仅支持 PNG、JPG、GIF 和 WebP 图片")
            first = ImageOps.exif_transpose(source).convert("RGBA")
            loop = source.info.get("loop")
            # QQ 表情常用最大循环值表示持续播放，与审核样张保持一致。
            if loop == 65535:
                loop = 0
            if not getattr(source, "is_animated", False):
                return SayingMedia(data, extension, first, [], [], loop)
            fallback = ""
            if source.format != "GIF":
                fallback = "该格式动图已保存静态预览，可用原图指令查看动态素材。"
            else:
                pixels = 0
                for index in range(SAYING_GIF_MAX_FRAMES + 1):
                    try:
                        source.seek(index)
                    except EOFError:
                        break
                    duration = max(10, int(source.info.get("duration", 100) or 100))
                    # GIF 时长以 10ms 编码，先归一化再合并时间轴，避免累计丢时长。
                    duration = max(10, round(duration / 10) * 10)
                    if index == SAYING_GIF_MAX_FRAMES or sum(durations) + duration > SAYING_GIF_MAX_DURATION:
                        fallback = "动图超过处理上限，已保存静态预览；原始动图仍可查看。"
                        break
                    frame = source.convert("RGBA")
                    frame.thumbnail((SAYING_TEXT_RIGHT - SAYING_TEXT_LEFT,
                                     SAYING_QUOTE_BOTTOM - SAYING_QUOTE_TOP), Image.Resampling.LANCZOS)
                    pixels += frame.width * frame.height
                    if pixels > frame_budget:
                        frame.close()
                        fallback = "动图超过处理上限，已保存静态预览；原始动图仍可查看。"
                        break
                    frames.append(frame)
                    durations.append(duration)
            if fallback:
                for frame in frames:
                    frame.close()
                frames, durations = [], []
            return SayingMedia(data, extension, first, frames, durations, loop, fallback)
    except BaseException:
        if first is not None:
            first.close()
        for frame in frames:
            frame.close()
        raise


async def _prepare_saying_segments(ctx, segments):
    """保留原序和换行，异步取素材，再在线程中生成内容指纹与解码结果。"""
    prepared = []
    media = []
    total_bytes = 0
    remaining_pixels = SAYING_GIF_MAX_PIXELS
    decoded_pixels = 0
    text_chars = 0
    try:
        for segment in segments:
            kind = _classify_saying_segment(segment)
            fields = dict(segment["data"])
            if kind == "reply":
                continue
            if kind == "at":
                target = str(fields.get("qq", ""))
                name = "全体成员" if target == "all" else target
                if target.isdigit():
                    try:
                        name = await get_group_member_name(ctx.group_id, int(target))
                    except Exception:
                        pass
                fields["display_name"] = name
            elif kind == "text":
                fields["text"] = fields.get("text", "").replace("\r\n", "\n").replace("\r", "\n")
                text_chars += len(fields["text"])
                if text_chars > SAYING_MAX_INPUT_CHARS:
                    raise ReplyException("消息内容较多，无法在单张语录中完整展示，请缩短后重试。")
            elif kind in ("image", "sticker", "emoji"):
                if len(media) >= SAYING_MEDIA_MAX_COUNT:
                    raise ReplyException("消息内容较多，无法在单张语录中完整展示，请缩短后重试。")
                data = await _download_saying_media(ctx.bot, segment)
                total_bytes += len(data)
                if total_bytes > SAYING_MEDIA_TOTAL_BYTES:
                    raise ReplyException("消息中的图片文件过大，请减少图片后重试。")
                decode_task = asyncio.create_task(run_in_pool(_decode_saying_media, data, remaining_pixels))
                try:
                    decoded = await asyncio.shield(decode_task)
                except asyncio.CancelledError:
                    # 线程中的 Pillow 不随协程取消而中止，必须等它结束再释放图像。
                    decoded = await decode_task
                    decoded.close()
                    raise
                decoded_pixels += decoded.first.width * decoded.first.height
                if decoded_pixels > SAYING_MEDIA_MAX_PIXELS * 2:
                    decoded.close()
                    raise ReplyException("消息中的图片分辨率过大，请减少图片后重试。")
                remaining_pixels -= sum(frame.width * frame.height for frame in decoded.frames)
                fields["_media"] = decoded
                fields["_source"] = len(media)
                media.append(decoded)
            else:
                raise ReplyException("这条消息包含暂不支持的内容，请使用文字、图片或表情生成语录。")
            prepared.append({"type": segment["type"], "data": fields})
        if not media and not any(item["type"] == "at" or item["data"].get("text", "").strip()
                                 for item in prepared):
            raise ReplyException("回复的消息没有文本或图片！")
        key = await run_in_pool(_saying_content_key, prepared) if media else ""
        return prepared, media, key
    except BaseException:
        for item in media:
            item.close()
        raise


def _saying_content_key(segments) -> str:
    """按作者之外的有序正文、@身份与媒体原始内容去重，忽略时效 URL 和文件名。"""
    tokens = []
    for segment in segments:
        kind = _classify_saying_segment(segment)
        fields = segment["data"]
        if kind == "text":
            value = fields["text"]
            if tokens and tokens[-1][0] == "text":
                tokens[-1][1] += value
            else:
                tokens.append([kind, value])
        elif kind == "at":
            tokens.append([kind, str(fields["qq"])])
        elif "_media" in fields:
            tokens.append([kind, hashlib.sha256(fields["_media"].data).hexdigest()])
    return hashlib.sha256(json.dumps(tokens, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def _saying_summary(segments) -> str:
    """以已解析的群友名称生成索引正文，避免对 @全体再次查询成员接口。"""
    text = ""
    for segment in segments:
        kind = _classify_saying_segment(segment)
        fields = segment['data']
        if kind == 'text':
            text += fields['text']
        elif kind == 'at':
            text += (' ' if text else '') + '@' + fields['display_name'] + ' '
        elif kind in ('image', 'sticker'):
            text += '[图片]'
        elif kind == 'emoji':
            text += '[表情]'
    return text


# ================================ 审核版固定图文排版 ================================ #

WIDTH = SAYING_CANVAS_SIZE[0]
HEIGHT = SAYING_CANVAS_SIZE[1]
LEFT = SAYING_TEXT_LEFT
RIGHT = SAYING_TEXT_RIGHT
CONTENT_WIDTH = RIGHT - LEFT
TOP = SAYING_QUOTE_TOP
MAX_CONTENT_HEIGHT = SAYING_QUOTE_BOTTOM - TOP
IMAGE_GAP = 20
ROW_GAP = 12
TEXT_SIZE = 46
MIN_TEXT_SIZE = 34
MIN_IMAGE_HEIGHT = 112
MIN_IMAGE_WIDTH = 96
LINE_HEIGHT = round(TEXT_SIZE * SAYING_LINE_SPACING_RATIO)
# 原生小表情使用字高；普通图与大表情保留完整预览区域，身份取自消息协议。
INLINE_IMAGE_HEIGHT = min(TEXT_SIZE, get_font_std_size(
    get_font(DEFAULT_FONT, TEXT_SIZE))[1])


class QuoteCapacityError(ValueError):
    """固定横版无法完整容纳可辨认内容时，交给命令层返回明确提示。"""


def _classify_saying_segment(segment):
    """按协议区分原生表情、大表情和普通图，缺少标记时保守保留普通图身份。"""
    kind = segment['type']
    if kind == 'face':
        return 'emoji'
    if kind == 'mface':
        return 'sticker'
    if kind == 'image':
        data = segment['data']
        subtype = data.get('sub_type')
        if subtype is None:
            subtype = data.get('subType')
        return 'sticker' if str(subtype) == '1' else 'image'
    return kind

def _prepare_saying_units(segments):
    """将已解码素材转为排版单元，表情身份仅依据消息协议。"""
    units = []
    font = get_font(DEFAULT_FONT, TEXT_SIZE)
    for segment in segments:
        kind = _classify_saying_segment(segment)
        fields = segment['data']
        if kind == 'at':
            fields = {'text': '@' + fields['display_name']}
            kind = 'text'
        if kind == 'text':
            text = fields['text']
            if not text.strip() and '\n' not in text:
                continue
            for unit_text, _ in get_inline_text_layout_units(text):
                units.append({'type': 'text', 'text': unit_text, 'height': LINE_HEIGHT,
                              'width': get_text_width(font, unit_text)})
        elif kind in ('image', 'sticker', 'emoji'):
            media = fields['_media']
            image = media.first.copy()
            is_emoji = kind == 'emoji'
            if is_emoji:
                visible_bounds = image.getchannel('A').getbbox()
                if visible_bounds:
                    cropped = image.crop(visible_bounds)
                    image.close()
                    image = cropped
            target_height = INLINE_IMAGE_HEIGHT if is_emoji else MAX_CONTENT_HEIGHT
            upscale_limit = max(1, min(3, 183 / image.height)) if media.extension == 'gif' and not is_emoji else 1
            scale = min(upscale_limit, CONTENT_WIDTH / image.width, target_height / image.height)
            units.append({'type': 'image', 'image': image,
                          'height': round(image.height * scale), 'width': round(image.width * scale),
                          'inline': is_emoji, 'role': kind, 'upscale_limit': upscale_limit,
                          'source': fields['_source']})
    return units


def _layout_saying_inline_units(units, line_height=LINE_HEIGHT):
    """同一行从左到右排图文，原文换行后另起一行，合并连续文字用于绘制。"""
    rows = []
    current = []
    current_width = 0
    for unit in units:
        if unit['type'] == 'text' and unit['text'] == '\n':
            rows.append((current, current_width))
            current = []
            current_width = 0
            continue
        gap = IMAGE_GAP if current and (unit['type'] == 'image' or current[-1]['type'] == 'image') else 0
        if current and current_width + gap + unit['width'] > CONTENT_WIDTH:
            rows.append((current, current_width))
            current = []
            current_width = 0
            gap = 0
        if not current and unit['type'] == 'text' and unit['text'].isspace():
            continue
        left = current_width + gap
        if current and current[-1]['type'] == 'text' and unit['type'] == 'text':
            current[-1]['text'] += unit['text']
            current[-1]['width'] += unit['width']
        else:
            current.append({**unit, 'left': left})
        current_width = left + unit['width']
    if current:
        rows.append((current, current_width))
    heights = [max((unit['height'] for unit in row), default=line_height) for row, _ in rows]
    return rows, heights, sum(heights) + ROW_GAP * max(0, len(rows) - 1)


def _fit_saying_image_rows(units):
    """按字号和图片尺寸的有限候选排版，达到尺寸下限后明确结束。"""
    has_pictures = any(unit['type'] == 'image' and not unit['inline'] for unit in units)
    image_limits = range(MAX_CONTENT_HEIGHT, MIN_IMAGE_HEIGHT - 1, -1) if has_pictures else [MAX_CONTENT_HEIGHT]
    # ================================ 可读尺寸约束与字号自适应 ================================ #
    # 逐项试算而不假定换行结果单调；只计算尺寸，最终成品只缩放一次原图。
    for text_size in range(TEXT_SIZE, MIN_TEXT_SIZE - 1, -2):
        font = get_font(DEFAULT_FONT, text_size)
        line_height = round(text_size * SAYING_LINE_SPACING_RATIO)
        emoji_height = min(text_size, get_font_std_size(font)[1])
        measured_units = []
        for unit in units:
            candidate = dict(unit)
            if unit['type'] == 'text':
                candidate['width'] = get_text_width(font, unit['text'])
                candidate['height'] = line_height
            elif unit['inline']:
                image = unit['image']
                scale = min(1, emoji_height / image.height, text_size * 2 / image.width)
                candidate['width'] = max(1, round(image.width * scale))
                candidate['height'] = max(1, round(image.height * scale))
            measured_units.append(candidate)
        for image_height in image_limits:
            candidates = []
            too_small = False
            for unit in measured_units:
                candidate = dict(unit)
                if unit['type'] == 'image' and not unit['inline']:
                    image = unit['image']
                    scale = min(unit.get('upscale_limit', 1), CONTENT_WIDTH / image.width, image_height / image.height)
                    candidate['width'] = max(1, round(image.width * scale))
                    candidate['height'] = max(1, round(image.height * scale))
                    if (candidate['height'] < min(MIN_IMAGE_HEIGHT, unit['height'])
                            or candidate['width'] < min(MIN_IMAGE_WIDTH, unit['width'])):
                        too_small = True
                        break
                candidates.append(candidate)
            if too_small:
                continue
            rows, heights, content_height = _layout_saying_inline_units(candidates, line_height)
            if content_height <= MAX_CONTENT_HEIGHT:
                return rows, heights, content_height, font, line_height
    raise QuoteCapacityError('消息内容较多，无法在单张语录中完整展示，请缩短后重试。')


def _render_rich_saying(avatar, author, segments):
    """固定横版内按消息原序排图文，同一条消息生成一张卡。"""
    # 纯文字仍调用原实现，避免这次图文扩展改变既有卡面。
    if all(segment['type'] == 'text' for segment in segments):
        text = ''.join(segment['data']['text'] for segment in segments)
        return [_render_saying_image(avatar, text, author)], []

    units = _prepare_saying_units(segments)
    try:
        rows, heights, content_height, font, line_height = _fit_saying_image_rows(units)
    except Exception:
        for unit in units:
            if unit['type'] == 'image':
                unit['image'].close()
        raise
    only_images = all(unit['type'] == 'image' or unit['text'].isspace() for unit in units)
    layout_records = []
    author_text, author_font = _fit_saying_author(author)
    # ================================ 原横版头像与渐变 ================================ #
    canvas = Image.new('RGBA', (WIDTH, HEIGHT), BLACK)
    canvas.paste(_crop_saying_avatar(avatar), (0, 0))
    canvas.alpha_composite(_build_saying_gradient_overlay())
    painter = Painter(img=canvas)
    row_top = TOP + (MAX_CONTENT_HEIGHT - content_height) // 2

    # ================================ 原序图文行内排版 ================================ #
    for (row, row_width), row_height in zip(rows, heights):
        # 纯图组合整体居中；混排从正文左边起笔，图片跟在原文本位置。
        row_left = LEFT + round((CONTENT_WIDTH - row_width) / 2) if only_images else LEFT
        row_text = ''.join(block['text'] for block in row if block['type'] == 'text')
        text_top = row_top + max(0, (row_height - line_height) // 2)
        for block in row:
            left = round(row_left + block['left'])
            top = row_top
            if block['type'] == 'text':
                top = text_top
                painter._text(block['text'], (left, top), font, WHITE)
                layout_records.append({'type': 'text', 'text': block['text'],
                                       'left': left, 'top': top,
                                       'width': round(block['width']), 'height': block['height']})
                layout_records[-1]['font_size'] = font.size
            else:
                original = block['image']
                if block['inline']:
                    # 透明边距已裁掉，按实际字形中心对齐，避免图形因基线和留白而上浮。
                    glyph_bounds = font.getbbox(row_text or '哇', anchor='ls')
                    glyph_center = text_top + get_font_std_size(font)[1] + (
                        glyph_bounds[1] + glyph_bounds[3]) / 2
                    top = round(glyph_center - block['height'] / 2)
                else:
                    # 同排图片按可用行高居中，让较小的动图和较高的截图相互对齐。
                    top += (row_height - block['height']) // 2
                image = original.resize((block['width'], block['height']), Image.Resampling.LANCZOS)
                canvas.paste(image, (left, top), image)
                layout_records.append({'type': 'image',
                                       'source': block['source'], 'left': left, 'top': top,
                                       'width': image.width, 'height': image.height})
                layout_records[-1]['role'] = block['role']
                image.close()
        row_top += row_height + ROW_GAP

    # ================================ 原署名位置 ================================ #
    author_left = round(RIGHT - get_text_width(author_font, author_text))
    author_top = SAYING_AUTHOR_BOTTOM - round(author_font.size * SAYING_LINE_SPACING_RATIO)
    painter._text(author_text, (author_left, author_top), author_font, (225, 225, 225, 255))
    result = painter.img.convert('RGB')
    canvas.close()
    for unit in units:
        if unit['type'] == 'image':
            unit['image'].close()
    return [result], layout_records




# ================================ 有界时间轴与动图编码 ================================ #

def _build_saying_palette(base, tiles):
    """将动态图常用色和静态底板放入同一调色板，使头像和文字逐帧保持稳定。"""
    colors = Counter()
    for tile in tiles:
        for count, color in tile.getcolors(tile.width * tile.height):
            colors[color] += count
        # 大型视频式 GIF 可能有大量不同颜色，约束调色板候选而非长期保留所有像素颜色。
        if len(colors) > 65536:
            colors = Counter(dict(colors.most_common(65536)))
    animation_colors = [color for color, _ in colors.most_common(128)]
    background_colors = 256 - len(animation_colors)
    quantized = base.quantize(colors=background_colors, method=Image.Quantize.MEDIANCUT)
    palette_values = [channel for color in animation_colors for channel in color]
    palette_values += quantized.getpalette()[:background_colors * 3]
    palette_values += [0] * (768 - len(palette_values))
    palette = Image.new('P', (1, 1))
    palette.putpalette(palette_values)
    quantized.close()
    return palette




def _build_saying_timeline(sources):
    """完整保留有限循环；连续播放多源按共同周期对齐，超过上限则静态回退。"""
    cycles = [sum(source.durations) for source in sources]
    if len(sources) == 1:
        total, loop = cycles[0], sources[0].loop
    elif all(source.loop == 0 for source in sources):
        total, loop = math.lcm(*cycles), 0
    elif len(set(cycles)) == 1 and len({source.loop for source in sources}) == 1:
        total, loop = cycles[0], sources[0].loop
    else:
        raise ValueError("多个动图播放次数不同，已保存静态预览；原始动图仍可查看。")
    if total > SAYING_GIF_MAX_DURATION:
        raise ValueError("动图合成周期超过处理上限，已保存静态预览；原始动图仍可查看。")
    boundaries = {0, total}
    source_starts = []
    for source, cycle in zip(sources, cycles):
        starts = []
        elapsed = 0
        for duration in source.durations:
            starts.append(elapsed)
            elapsed += duration
        source_starts.append(starts)
        for cycle_start in range(0, total, cycle):
            boundaries.update(cycle_start + start for start in starts if cycle_start + start < total)
            if len(boundaries) - 1 > SAYING_GIF_MAX_FRAMES:
                raise ValueError("合成动图帧数超过处理上限，已保存静态预览；原始动图仍可查看。")
    boundaries = sorted(boundaries)
    return boundaries[:-1], [end - start for start, end in zip(boundaries, boundaries[1:])], source_starts, loop


def _encode_saying_animation(static, layout, media):
    """静态底板只量化一次，动态图共享调色板并采用差分帧编码。"""
    animated = [block for block in layout if block['type'] == 'image' and media[block['source']].frames]
    if not animated:
        return _encode_saying_image(static), 'png', ''
    sources = [media[block['source']] for block in animated]
    try:
        starts, durations, source_starts, loop = _build_saying_timeline(sources)
    except ValueError as error:
        return _encode_saying_image(static), 'png', str(error)
    base = static.copy()
    tracks = []
    indexed_tracks = []
    frames = []
    palette = indexed_base = None
    try:
        draw = ImageDraw.Draw(base)
        for block in animated:
            draw.rectangle((block['left'], block['top'], block['left'] + block['width'] - 1,
                            block['top'] + block['height'] - 1), fill=(0, 0, 0))
        for block, source in zip(animated, sources):
            box = (block['left'], block['top'], block['left'] + block['width'], block['top'] + block['height'])
            track = []
            tracks.append(track)
            for frame in source.frames:
                resized = frame.resize((block['width'], block['height']), Image.Resampling.LANCZOS)
                tile = base.crop(box)
                tile.paste(resized, (0, 0), resized)
                track.append(tile)
                resized.close()
        palette = _build_saying_palette(base, [tile for track in tracks for tile in track])
        indexed_base = base.quantize(palette=palette, dither=Image.Dither.FLOYDSTEINBERG)
        for track in tracks:
            indexed_tracks.append([tile.quantize(palette=palette, dither=Image.Dither.NONE) for tile in track])
        for timestamp in starts:
            frame = indexed_base.copy()
            for block, source, track, source_start in zip(animated, sources, indexed_tracks, source_starts):
                index = bisect_right(source_start, timestamp % sum(source.durations)) - 1
                frame.paste(track[index], (block['left'], block['top']))
            frames.append(frame)
        output = io.BytesIO()
        loop_args = {'loop': loop} if loop is not None else {}
        frames[0].save(output, format='GIF', save_all=True, append_images=frames[1:],
                       duration=durations, disposal=1, optimize=True, **loop_args)
        data = output.getvalue()
        if len(data) > SAYING_OUTPUT_MAX_BYTES:
            return _encode_saying_image(static), 'png', '生成的动图文件过大，已保存静态预览；原始动图仍可查看。'
        return data, 'gif', ''
    finally:
        base.close()
        if palette is not None:
            palette.close()
        if indexed_base is not None:
            indexed_base.close()
        for image in [*frames, *(tile for track in tracks + indexed_tracks for tile in track)]:
            image.close()


def _render_and_encode_rich_saying(avatar, author, segments, media):
    """在同一工作线程完成排版与编码，释放绘图资源后返回字节和回退提示。"""
    pages, layout = _render_rich_saying(avatar, author, segments)
    image = pages[0]
    try:
        data, extension, fallback = _encode_saying_animation(image, layout, media)
        notices = list(dict.fromkeys([item.fallback for item in media if item.fallback] + ([fallback] if fallback else [])))
        return data, extension, '\n'.join(notices)
    finally:
        image.close()


# ================================ 语录存储与图片册 ================================ #

saying_store = SayingStore("data/imgtool/sayings.sqlite", "data/imgtool/sayings")


async def _close_saying_store():
    await saying_store.close()


def _encode_saying_image(image: Image.Image) -> bytes:
    """在绘图工作线程中编码无损 PNG。"""
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=False, compress_level=6)
    return output.getvalue()


def _encode_saying_album(image: Image.Image) -> bytes:
    """在绘图工作线程中编码图册；完整色度采样兼顾头像和小字清晰度。"""
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=SAYING_ALBUM_JPEG_QUALITY,
               subsampling=0, optimize=False)
    return output.getvalue()


def _get_saying_settings() -> dict:
    """在线程中读取配置并归一化收录容量和图册页大小。"""
    return {
        'max_per_group': max(1, int(config.get('sayings.max_per_group', 3000))),
        'max_total_bytes': max(1, int(config.get('sayings.max_total_mb', 1024))) * 1024 * 1024,
        'page_size': max(1, min(SAYING_ALBUM_MAX_PAGE_SIZE,
                              int(config.get('sayings.page_size', SAYING_ALBUM_MAX_PAGE_SIZE)))),
    }


def _render_saying_album(entries, author_name: str, author_id: int | None,
                         total: int, page: int, page_count: int, theme: str = "light") -> Image.Image:
    """按选定配色生成个人或全群图册，整图缩略展示并提供操作指令。"""
    # ================================ 图册框架 ================================ #
    colors = SAYING_ALBUM_THEMES[theme]
    margin = 24
    header_height = 96
    caption_height = 30
    gap = 20
    card_padding = 10
    guide_lines = ["用法：/语录 [编号] ；/查语录 @[目标]"]
    if page_count > 1:
        query_target = "all" if author_id is None else str(author_id)
        theme_suffix = " 深色" if theme == "dark" else ""
        guide_lines.append(f"翻页：/查语录 {query_target} p2{theme_suffix}")
    footer_height = 36 + len(guide_lines) * 28
    image_width, image_height = SAYING_ALBUM_IMAGE_SIZE
    card_width = image_width + card_padding * 2
    card_height = caption_height + image_height + card_padding * 2
    columns = min(SAYING_ALBUM_COLUMNS, max(1, len(entries)))
    rows = (len(entries) + columns - 1) // columns
    width = margin * 2 + columns * card_width + (columns - 1) * gap
    content_height = rows * card_height + max(0, rows - 1) * gap
    height = header_height + content_height + footer_height
    canvas = Image.new("RGB", (width, height), colors["background"])
    painter = Painter(img=canvas)
    title_font = get_font(DEFAULT_FONT, 32)
    if author_id is None:
        title = "本群语录"
    else:
        title = f"{author_name}的语录"
        while get_text_width(title_font, title) > width - margin * 2 and len(author_name) > 1:
            author_name = author_name[:-1]
            title = f"{author_name}…的语录"
    painter._text(title, (margin, 16), title_font, colors["title"])
    painter._text(f"本群收录 {total} 条",
                  (margin, 60), get_font(DEFAULT_FONT, 20), colors["secondary"])

    # ================================ 语录卡片 ================================ #
    number_font = get_font(DEFAULT_FONT, 22)
    card_draw = ImageDraw.Draw(canvas)
    for index, (saying, png) in enumerate(entries):
        row_index, column = divmod(index, columns)
        left = margin + column * (card_width + gap)
        top = header_height + row_index * (card_height + gap)
        card_draw.rounded_rectangle((left, top, left + card_width - 1, top + card_height - 1),
                                    radius=14, fill=colors["card"], outline=colors["border"], width=1)
        painter._text(f"#{saying.number}", (left + card_padding, top + card_padding),
                      number_font, colors["number"])
        image_left = left + card_padding
        image_top = top + card_padding + caption_height
        try:
            if png is None:
                raise FileNotFoundError()
            with Image.open(io.BytesIO(png)) as image:
                # 图册展示等比首帧缩略图，单张调用发送保存的 PNG 或 GIF。
                if image.size != SAYING_CANVAS_SIZE:
                    raise ValueError("语录图片尺寸不匹配")
                with image.resize(SAYING_ALBUM_IMAGE_SIZE, Image.Resampling.LANCZOS) as thumbnail:
                    canvas.paste(thumbnail, (image_left, image_top))
        except (OSError, ValueError):
            painter._text("原图已丢失或损坏", (image_left + 20, image_top + 80),
                          get_font(DEFAULT_FONT, 24), colors["secondary"])

    # ================================ 查询引导 ================================ #
    for index, line in enumerate(guide_lines):
        painter._text(line, (margin, header_height + content_height + 18 + index * 28),
                      get_font(DEFAULT_FONT, 18), colors["secondary"])
    page_font = get_font(DEFAULT_FONT, 18)
    page_label = f"{page}/{page_count}"
    page_position = (width - margin - get_text_width(page_font, page_label),
                     header_height + content_height + 18 + (len(guide_lines) - 1) * 28)
    painter._text(page_label, page_position, page_font, colors["secondary"])
    return canvas


async def _get_saying_image_cq(data: bytes, extension: str = "png") -> str:
    """在线程中准备对应格式的发送文件，再生成本地图片消息。"""
    def create_snapshot():
        return TempFilePath(extension, remove_after=timedelta(
            minutes=global_config.get('msg_send.tmp_img_keep_minutes')))
    snapshot = await run_in_pool(create_snapshot)
    with snapshot as path:
        await run_in_pool(Path(path).write_bytes, data)
        # TempFilePath 已提供绝对路径，延迟清理登记由事件循环完成。
        return f'[CQ:image,file=file://{path}]'


async def _send_saved_saying(ctx: HandlerContext, stored, *, created: bool | None = None):
    """发送原图快照，区分首次收录、重复命中和按编号查看。"""
    if stored is None:
        raise ReplyException("本群没有这条语录")
    saying, png = stored
    if png is None:
        raise ReplyException(f"语录 #{saying.number} 的原图已丢失，请删除记录后重新收录")
    def verify_image():
        with Image.open(io.BytesIO(png)) as image:
            image.verify()
    try:
        await run_in_pool(verify_image)
    except (OSError, ValueError, SyntaxError):
        raise ReplyException(f"语录 #{saying.number} 的原图已损坏，请删除记录后重新收录")
    image_cq = await _get_saying_image_cq(png, saying.extension)
    if created is False:
        notice = f"这条语录已收录，本群编号 #{saying.number}。"
    else:
        prefix = "已收录为" if created else ""
        notice = f"{prefix}本群语录 #{saying.number}"
    return await ctx.asend_reply_msg(f"{notice}\n{image_cq}")


async def _get_saying_source(ctx: HandlerContext):
    """提取原作者与有序消息段，合并转发取第一个节点的作者。"""
    reply_msg = ctx.get_reply_msg()
    if not reply_msg or ctx.get_reply_sender() is None:
        raise ReplyException("请回复一条消息使用 /语录")
    reply_user_id = ctx.get_reply_sender().user_id
    reply_user_name = get_user_name_by_event(ctx.event.reply)
    reply_cqs = extract_cq_code(reply_msg)
    if 'forward' in reply_cqs:
        forward = reply_cqs['forward'][0]
        nodes = forward.get('content')
        if not nodes and forward.get('id'):
            nodes = (await get_forward_msg(ctx.bot, forward['id']))['messages']
        if not nodes:
            raise ReplyException("无法获取合并转发中的原消息")
        node = nodes[0]
        if node.get('type') == 'node':
            node = node['data']
        sender = node.get('sender') or node
        reply_user_id = sender.get('user_id') or sender.get('uin')
        if not reply_user_id or not str(reply_user_id).isdigit():
            raise ReplyException("无法确认合并转发中原作者的 QQ，不能收录到转发者名下")
        reply_user_name = sender.get('card') or sender.get('nickname') or node.get('name') or str(reply_user_id)
        reply_msg = node.get('message') or node.get('content')
        if isinstance(reply_msg, str):
            reply_msg = process_msg_segs(Message(reply_msg))
    return int(reply_user_id), reply_user_name, reply_msg


async def _send_saying_originals(ctx, stored):
    """从本群已保存记录发送源图片，GIF 保持动态，长截图可查看完整尺寸。"""
    if stored is None:
        raise ReplyException("本群没有这条语录")
    saying, _ = stored
    metadata = json.loads(saying.media_json)
    if not metadata:
        raise ReplyException("这条语录没有收录的图片素材，请使用 /语录 编号 查看语录图")
    try:
        originals = await saying_store.read_media(saying)
    except OSError:
        raise ReplyException("这条语录的图片素材已丢失或无法读取")
    for item, data in zip(metadata, originals):
        image_cq = await _get_saying_image_cq(data, item['extension'])
        await ctx.asend_reply_msg(image_cq)


# ================================ 生成与调用语录 ================================ #

async def _handle_saying(ctx: HandlerContext):
    args = ctx.get_args().strip()
    if args:
        if ctx.get_reply_msg_id() is not None:
            raise ReplyException("生成语录时请回复消息直接发送 /语录，不要附加编号")
        try:
            show_originals = args.endswith(" 原图")
            number = parse_saying_number(args[:-3] if show_originals else args)
        except ValueError as error:
            raise ReplyException(str(error))
        stored = await saying_store.get(ctx.group_id, number)
        if show_originals:
            return await _send_saying_originals(ctx, stored)
        return await _send_saved_saying(ctx, stored)
    source_msg_id = ctx.get_reply_msg_id()
    if source_msg_id is None:
        raise ReplyException("请回复消息生成语录，或使用 /语录 编号 查看原图")

    # 优先复用原消息已有记录。索引服务异常时继续生成即时语录图。
    try:
        stored = await saying_store.find_source(ctx.group_id, source_msg_id)
    except Exception:
        logger.print_exc("检查语录收录记录失败")
        stored = None
    if stored is not None:
        return await _send_saved_saying(ctx, stored, created=False)
    # 下载、解码与动画合成都在同一限流范围，排队时尚未持有大图资源。
    async with _saying_render_slots:
        return await _collect_rich_saying(ctx, source_msg_id)


async def _collect_rich_saying(ctx, source_msg_id):
    """收录有序图文，内容重复复用快照；存储异常仍发送即时成品。"""
    try:
        reply_user_id, reply_user_name, segments = await _get_saying_source(ctx)
    except ReplyException:
        raise
    except Exception:
        logger.print_exc("生成语录获取回复消息失败")
        raise ReplyException("无法获取回复消息")
    try:
        prepared, media, content_key = await _prepare_saying_segments(ctx, segments)
    except ReplyException:
        raise
    except Exception as error:
        logger.print_exc("读取语录图片失败")
        if isinstance(error, ValueError):
            raise ReplyException(str(error))
        raise ReplyException("无法读取消息中的图片或表情，请重新发送后再收录。")
    try:
        text = _saying_summary(prepared)
        # 纯文字沿用旧正文查重，图文必须同时匹配媒体内容和插入顺序。
        try:
            stored = await saying_store.find_content(ctx.group_id, reply_user_id, text, content_key)
        except Exception:
            logger.print_exc("检查语录正文收录记录失败")
            stored = None
        if stored is not None:
            return await _send_saved_saying(ctx, stored, created=False)
        avatar = await download_image(await get_avatar_url_large(ctx.bot, reply_user_id))
        try:
            if not media:
                prepared = [{'type': 'text', 'data': {'text': text}}]
            render_task = asyncio.create_task(run_in_pool(
                _render_and_encode_rich_saying, avatar, reply_user_name, prepared, media))
            try:
                png, extension, fallback = await asyncio.shield(render_task)
            except asyncio.CancelledError:
                await render_task
                raise
            except QuoteCapacityError as error:
                raise ReplyException(str(error))
        finally:
            avatar.close()
        source_media = tuple((item.extension, item.data) for item in media)
    finally:
        for item in media:
            item.close()
    try:
        settings = await run_in_pool(_get_saying_settings)
        saying, created = await saying_store.save(
            group_id=ctx.group_id, author_id=reply_user_id, nickname=reply_user_name,
            text=text, source_msg_id=source_msg_id, recorder_id=ctx.user_id, png=png,
            max_per_group=settings['max_per_group'],
            max_total_bytes=settings['max_total_bytes'],
            content_key=content_key, extension=extension, media=source_media,
        )
    except SayingLimitError as error:
        notice = f"{error}，图片已生成但未收录"
    except Exception:
        logger.print_exc("保存语录失败")
        notice = "图片已生成，但保存失败，未收录到语录册"
    else:
        stored = (saying, png) if created else await saying_store.get(ctx.group_id, saying.number)
        if created and fallback:
            await ctx.asend_reply_msg(fallback)
        return await _send_saved_saying(ctx, stored, created=created)
    if fallback:
        notice += '\n' + fallback
    image_cq = await _get_saying_image_cq(png, extension)
    return await ctx.asend_reply_msg(f"{notice}\n{image_cq}")


# ================================ 查询与删除语录 ================================ #

async def _handle_query_sayings(ctx: HandlerContext):
    mentioned_ids = []
    for segment in ctx.get_msg():
        if segment['type'] == 'at':
            value = str(segment['data'].get('qq', ''))
            if value == str(ctx.bot.self_id):
                continue
            if not value.isdigit():
                raise ReplyException("请 @ 一位群友，不支持 @全体成员")
            mentioned_ids.append(int(value))
    try:
        author_id, page, theme = parse_saying_query(ctx.get_args(), mentioned_ids, ctx.user_id)
        # 双列图册每页最多 16 条，通过分页约束输出高度与内存占用。
        settings = await run_in_pool(_get_saying_settings)
        entries, total, page_count = await saying_store.list_page(
            ctx.group_id, author_id, page, settings['page_size'])
    except (ValueError, SayingPageError) as error:
        raise ReplyException(str(error))
    if not entries:
        if author_id is None:
            raise ReplyException("本群还没有收录的语录，回复消息发送 /语录 即可收录")
        raise ReplyException("这位群友在本群还没有收录的语录")
    image = await run_in_pool(_render_saying_album, entries, entries[0][0].nickname,
                              author_id, total, page, page_count, theme)
    try:
        jpeg = await run_in_pool(_encode_saying_album, image)
    finally:
        image.close()
    image_cq = await _get_saying_image_cq(jpeg, "jpg")
    return await ctx.asend_reply_msg(image_cq)


async def _handle_delete_saying(ctx: HandlerContext):
    try:
        number = parse_saying_number(ctx.get_args())
    except ValueError as error:
        raise ReplyException(str(error).replace('/语录 12', '/删除语录 12'))
    stored = await saying_store.get(ctx.group_id, number)
    if stored is None:
        raise ReplyException("本群没有这条语录")
    saying, _ = stored
    is_admin = False
    if ctx.user_id not in (saying.author_id, saying.recorder_id):
        is_admin = await check_group_admin_or_superuser(ctx.bot, ctx.event)
    try:
        deleted = await saying_store.delete(ctx.group_id, number, ctx.user_id, is_admin)
    except PermissionError as error:
        raise ReplyException(str(error))
    if deleted is None:
        raise ReplyException("这条语录已被删除")
    _, cleanup_error = deleted
    if cleanup_error is not None:
        logger.error(f"删除语录 #{number} 后清理图片失败: {cleanup_error}")
        return await ctx.asend_reply_msg(f"已删除本群语录 #{number}，但图片文件清理失败，请联系管理员")
    return await ctx.asend_reply_msg(f"已删除本群语录 #{number}")

# ================================ 命令注册 ================================ #

def register_sayings(shared_config, shared_logger, shared_cd, shared_gbl):
    """复用 ImgTool 的配置、冷却和群开关，注册语录命令与关闭回调。"""
    global config, logger
    config = shared_config
    logger = shared_logger
    commands = (
        (['/saying', '/quote', '/语录'], _handle_saying),
        (['/查语录', '/查询语录'], _handle_query_sayings),
        (['/删除语录', '/删语录'], _handle_delete_saying),
    )
    for aliases, handler in commands:
        matcher = CmdHandler(aliases, logger)
        matcher.check_cdrate(shared_cd).check_wblist(shared_gbl).check_group()
        matcher.handle()(handler)
    on_shutdown()(_close_saying_store)
