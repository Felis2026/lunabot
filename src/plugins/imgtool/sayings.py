"""语录的生成绘图、持久收录、分页检索与群内命令。"""

from ..utils import *
from PIL import Image, ImageDraw, ImageEnhance
from functools import lru_cache
import io

import asyncio
from dataclasses import dataclass
from pathlib import Path
import re
import time

import aiosqlite


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
                await connection.commit()
            except BaseException:
                await connection.close()
                raise
            self._connection = connection
        return self._connection

    def image_path(self, saying: Saying) -> Path:
        """根据数据库中的群号和编号确定图片路径。"""
        return self.image_dir / str(saying.group_id) / f"{saying.number}.png"

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

    async def find_content(self, group_id: int, author_id: int, text: str) -> tuple[Saying, bytes | None] | None:
        """按同群作者与完整正文查重，复用最早收录的编号和原图。"""
        async with self._lock:
            connection = await self._connect()
            row = await self._fetch_one(connection, """
                SELECT * FROM sayings WHERE group_id = ? AND author_id = ? AND text = ?
                ORDER BY number LIMIT 1
            """, (group_id, author_id, text))
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
                   max_per_group: int, max_total_bytes: int) -> tuple[Saying, bool]:
        """保存成品图及索引，同群同作者的相同正文复用已有编号。"""
        async with self._lock:
            connection = await self._connect()
            image_path = None
            await connection.execute("BEGIN IMMEDIATE")
            try:
                row = await self._fetch_one(connection,
                    "SELECT * FROM sayings WHERE group_id = ? AND source_msg_id = ?",
                    (group_id, source_msg_id))
                # 写事务内再次查正文，避免不同消息并发收录时重复分配编号。
                if row is None:
                    row = await self._fetch_one(connection, """
                        SELECT * FROM sayings WHERE group_id = ? AND author_id = ? AND text = ?
                        ORDER BY number LIMIT 1
                    """, (group_id, author_id, text))
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
                if row["size"] + len(png) > max_total_bytes:
                    raise SayingLimitError("语录图片存储空间已达上限")

                row = await self._fetch_one(connection,
                    "SELECT last_number FROM saying_counters WHERE group_id = ?", (group_id,))
                number = row["last_number"] + 1 if row is not None else 1
                saying = Saying(group_id, number, author_id, nickname, text,
                                source_msg_id, recorder_id, int(time.time()), len(png))
                image_path = self.image_path(saying)

                # 原图写入完成后提交索引。请求取消时等待文件线程结束，随后清理未提交图片。
                def write_image():
                    image_path.parent.mkdir(parents=True, exist_ok=True)
                    image_path.write_bytes(png)

                write_task = asyncio.create_task(asyncio.to_thread(write_image))
                try:
                    await asyncio.shield(write_task)
                except asyncio.CancelledError:
                    await write_task
                    raise
                await connection.execute("INSERT INTO sayings VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (group_id, number, author_id, nickname, text, source_msg_id,
                     recorder_id, saying.created_at, len(png)))
                await connection.execute("""
                    INSERT INTO saying_counters VALUES (?, ?)
                    ON CONFLICT(group_id) DO UPDATE SET last_number = excluded.last_number
                """, (group_id, number))
                await connection.commit()
                return saying, True
            except BaseException:
                await connection.rollback()
                if image_path is not None:
                    await asyncio.to_thread(image_path.unlink, missing_ok=True)
                raise

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
                await asyncio.to_thread(self.image_path(saying).unlink, missing_ok=True)
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
                # 图册展示原图的等比缩略图，单张调用仍发送保存的原始 PNG。
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
    image_cq = await _get_saying_image_cq(png)
    if created is False:
        notice = f"这条语录已收录，本群编号 #{saying.number}。"
    else:
        prefix = "已收录为" if created else ""
        notice = f"{prefix}本群语录 #{saying.number}"
    return await ctx.asend_reply_msg(f"{notice}\n{image_cq}")


async def _get_saying_source(ctx: HandlerContext):
    """提取回复中的原作者与完整文本，合并转发取第一个节点的作者。"""
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
    text = await extract_special_text(reply_msg, ctx.group_id)
    if not text:
        raise ReplyException("回复的消息没有文本!")
    return int(reply_user_id), reply_user_name, text


# ================================ 生成与调用语录 ================================ #

async def _handle_saying(ctx: HandlerContext):
    args = ctx.get_args().strip()
    if args:
        if ctx.get_reply_msg_id() is not None:
            raise ReplyException("生成语录时请回复消息直接发送 /语录，不要附加编号")
        try:
            number = parse_saying_number(args)
        except ValueError as error:
            raise ReplyException(str(error))
        return await _send_saved_saying(ctx, await saying_store.get(ctx.group_id, number))
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
    try:
        reply_user_id, reply_user_name, text = await _get_saying_source(ctx)
    except ReplyException:
        raise
    except Exception:
        logger.print_exc("生成语录获取回复消息失败")
        raise ReplyException("无法获取回复消息")
    # 同一作者再次说出相同正文时，先复用首次收录的原图，省去头像请求和绘图。
    try:
        stored = await saying_store.find_content(ctx.group_id, reply_user_id, text)
    except Exception:
        logger.print_exc("检查语录正文收录记录失败")
        stored = None
    if stored is not None:
        return await _send_saved_saying(ctx, stored, created=False)
    avatar = await download_image(await get_avatar_url_large(ctx.bot, reply_user_id))
    try:
        image = await run_in_pool(_render_saying_image, avatar, text, reply_user_name)
    finally:
        avatar.close()
    try:
        png = await run_in_pool(_encode_saying_image, image)
    finally:
        image.close()
    try:
        settings = await run_in_pool(_get_saying_settings)
        saying, created = await saying_store.save(
            group_id=ctx.group_id, author_id=reply_user_id, nickname=reply_user_name,
            text=text, source_msg_id=source_msg_id, recorder_id=ctx.user_id, png=png,
            max_per_group=settings['max_per_group'],
            max_total_bytes=settings['max_total_bytes'],
        )
    except SayingLimitError as error:
        notice = f"{error}，图片已生成但未收录"
    except Exception:
        logger.print_exc("保存语录失败")
        notice = "图片已生成，但保存失败，未收录到语录册"
    else:
        stored = (saying, png) if created else await saying_store.get(ctx.group_id, saying.number)
        return await _send_saved_saying(ctx, stored, created=created)
    image_cq = await _get_saying_image_cq(png)
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
