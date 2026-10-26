from ...utils import *
from ..common import *
from ..handler import *
from ..asset import *
from ..draw import *
from .profile import (
    get_detailed_profile,
    get_detailed_profile_card_filter,
    get_detailed_profile_card,
)
from .card import (
    get_card_full_thumbnail,
    get_card_image,
    has_after_training,
    search_multi_cards,
    get_character_name_by_id,
)
from .event import (
    get_card_supply_type,
    get_chara_ban_events,
    get_event_banner_chara_id,
    parse_search_single_event_args,
)
from .resbox import get_res_icon


GACHA_LIST_PAGE_SIZE_CFG = config.item('gacha.list_page_size')

SINGLE_GACHA_HELP = """
单个卡池参数: 
123: 直接使用卡池编号
-2: 倒数第二个卡池
event123: 活动123对应的卡池
123 full: 查看本池四星或生日成员招募名单
123 mzk 蓝: 按查卡条件筛选招募名单
123 兑换: 查看成员自选券的四星兑换名单
123 缺卡: 查看本池未持有成员，需要抓包数据
123 兑换 缺卡 p2: 查看未持有兑换名单第二页
""".strip()
MULTI_GACHA_HELP = """
多个卡池参数:
p2: 查第二页卡池
去年/25年: 查指定年份的卡池
card123: 查包含指定卡牌123的卡池
复刻/回响: 查复刻或回响卡池
当前/leak: 查开放中或未来卡池
""".strip()

RERELEASE_KEYWORDS = ("[It's Back]", "[재등장]", "[复刻]", "[復刻]",)
ECHO_KEYWORDS = ("[回响]",)
GACHA_TYPE_NAMES = {
    'beginner': '新手',
    'normal': '一般',
    'ceil': '天井',
    'gift': '礼物',
}
GACHA_RATE_RARITIES = ['rarity_1', 'rarity_2', 'rarity_3', 'rarity_4', 'rarity_birthday']
GACHA_RARE_NAMES = {
    'rarity_1': '1星',
    'rarity_2': '2星',
    'rarity_3': '3星',
    'rarity_4': '4星',
    'rarity_birthday': '生日',
    'pickup': '当期',
}


@dataclass
class GachaBehavior:
    id: int
    type: str
    spin_count: int
    cost_type: Optional[int]
    cost_id: Optional[int]
    cost_quantity: Optional[int]
    execute_limit: Optional[int]
    colorful_pass: bool

@dataclass
class GachaCard:
    id: int
    weight: int
    is_wish: bool
    is_pickup: bool

@dataclass
class GachaCardRarityRate:
    rarity: str
    rate: int
    lottery_type: str

@dataclass
class Gacha:
    id: int
    name: str
    type: str
    summary: str
    desc: str
    start_at: datetime
    end_at: datetime
    asset_name: str
    ceilitem_id: Optional[int]
    rarity_rates: List[GachaCardRarityRate] = field(default_factory=list)
    behaviors: List[GachaBehavior] = field(default_factory=list)
    cards: List[GachaCard] = field(default_factory=list)
    raw: dict = field(default_factory=dict, repr=False, compare=False)

    def __contains__(self, key):
        return key == 'id'
    
    def __getitem__(self, key: str):
        return self.id if key == 'id' else None


@dataclass
class GachaFilter:
    page: Optional[int] = None
    year: Optional[int] = None
    card_id: Optional[int] = None
    is_rerelease: bool = False
    is_recall: bool = False
    is_current: bool = False
    is_leak: bool = False


@dataclass
class GachaDetailQuery:
    """单个卡池查询，条件和页码只作用于该池的四星名单。"""
    gacha: Gacha
    full: bool = False
    conditions: str = ''
    page: int = 1
    exchange: bool = False
    show_unowned: bool = False


@dataclass
class GachaCardWeightInfo:
    id: int
    rarity: str
    weight: int
    is_pickup: bool
    rate: float = 0.0
    guaranteed_rate: float = 0.0

@dataclass
class GachaWeightInfo:
    weights: Dict[str, int] = field(default_factory=dict)
    cards: Dict[str, List[GachaCardWeightInfo]] = field(default_factory=dict)
    rates: Dict[str, float] = field(default_factory=dict)
    guaranteed_rates: Dict[str, float] = field(default_factory=dict)
    guaranteed_type: Optional[str] = None
    guaranteed_rarities: List[str] = field(default_factory=list)

@dataclass
class GachaSpinResult:
    id: int
    rarity: str
    is_pickup: bool


# ======================= 处理逻辑 ======================= #

@MasterDataManager.map_function("gachas")
def gachas_map_fn(gachas):
    ret: List[Gacha] = []
    for item in gachas:
        g = Gacha(
            id=item['id'],
            name=item['name'],
            type=item['gachaType'],
            summary=item['gachaInformation'].get('summary', ""),
            desc=item['gachaInformation'].get('description', ""),
            start_at=datetime.fromtimestamp(item['startAt'] // 1000),
            end_at=datetime.fromtimestamp(item['endAt'] // 1000 + 1),
            asset_name=item['assetbundleName'],
            ceilitem_id=item.get('gachaCeilItemId'),
            raw=item,
        )
        for rate in item['gachaCardRarityRates']:
            g.rarity_rates.append(GachaCardRarityRate(
                rarity=rate['cardRarityType'],
                rate=rate['rate'],
                lottery_type=rate['lotteryType'],
            ))
        for behavior in item['gachaBehaviors']:
            g.behaviors.append(GachaBehavior(
                id=behavior['id'],
                type=behavior['gachaBehaviorType'],
                spin_count=behavior['spinCount'],
                cost_type=behavior.get('costResourceType'),
                cost_id=behavior.get('costResourceId'),
                cost_quantity=behavior.get('costResourceQuantity'),
                execute_limit=behavior.get('executeLimit'),
                colorful_pass=behavior.get('gachaSpinnableType', None) == "colorful_pass",
            ))
        pickup_ids = set()
        for pickup in item['gachaPickups']:
            pickup_ids.add(pickup['cardId'])
        for card in item['gachaDetails']:
            g.cards.append(GachaCard(
                id=card['cardId'],
                weight=card['weight'],
                is_wish=card.get('isWish', False),
                is_pickup=card['cardId'] in pickup_ids,
            ))
        ret.append(g)
    return ret

# 获取抽卡行为文本描述
def get_gacha_behavior_text(behavior: GachaBehavior | None) -> str:
    text = "未知类型"
    if not behavior:
        return text
    match behavior.type:
        case 'normal': text = "普通"
        case 'over_rarity_4_once': text = "保底4星"
        case 'over_rarity_3_once': text = "保底3星"
        case 'once_a_day': text = "每日"
        case 'once_a_week': text = "每周"
    if behavior.spin_count == 1:    text += "/单抽"
    elif behavior.spin_count == 10: text += "/十连"
    if behavior.colorful_pass:  text = "月卡" + text
    if behavior.execute_limit:
        text += f"(限{behavior.execute_limit}次)"
    return text

# 获取稀有度图片
async def get_rarity_img(ctx: SekaiHandlerContext, rarity: str) -> Optional[Image.Image]:
    if rarity == "rarity_birthday":
        rare_img = ctx.static_imgs.get(f"card/rare_birthday.png")
        rare_num = 1
    else:
        rare_img = ctx.static_imgs.get(f"card/rare_star_normal.png") 
        rare_num = int(rarity.split("_")[1])
    return await run_in_pool(concat_images, [rare_img] * rare_num, 'h')

# 获取卡池的banner
async def get_gacha_banner(ctx: SekaiHandlerContext, gacha_or_gacha_id: Union[Gacha, int], default=UNKNOWN_IMG) -> Optional[Image.Image]:
    if isinstance(gacha_or_gacha_id, Gacha):
        gacha_id = gacha_or_gacha_id.id
    else:
        gacha_id = gacha_or_gacha_id
    banner_path = f'home/banner/banner_gacha{gacha_id}/banner_gacha{gacha_id}.png'
    return await get_gacha_asset_image(ctx, banner_path, use_img_cache=True, default=default)

# 获取卡池的logo
async def get_gacha_logo(ctx: SekaiHandlerContext, gacha_or_gacha_id: Union[Gacha, int], default=UNKNOWN_IMG) -> Optional[Image.Image]:
    if isinstance(gacha_or_gacha_id, Gacha):
        gacha = gacha_or_gacha_id
    else:
        gacha = await ctx.md.gachas.find_by_id(gacha_or_gacha_id)
        assert_and_reply(gacha, f"找不到卡池{ctx.region.upper()}-{gacha_or_gacha_id}")
    return await get_gacha_asset_image(ctx, f"gacha/{gacha.asset_name}/logo/logo.png", use_img_cache=True, default=default)

# 获取卡池抽卡权重信息
async def get_gacha_weight_info(ctx: SekaiHandlerContext, gacha: Gacha) -> GachaWeightInfo:
    ret = GachaWeightInfo()
    for card in gacha.cards:
        rarity = (await ctx.md.cards.find_by_id(card.id))["cardRarityType"]
        info = GachaCardWeightInfo(
            id=card.id, 
            rarity=rarity,
            weight=card.weight,
            is_pickup=card.is_pickup,
        )
        ret.weights.setdefault(rarity, 0)
        ret.weights[rarity] += card.weight
        ret.cards.setdefault(rarity, []).append(info)
    # 保底类型
    for behavior in gacha.behaviors:
        if behavior.type == 'over_rarity_4_once':
            ret.guaranteed_type = 'rarity_4'
            ret.guaranteed_rarities = ['rarity_4']
        elif behavior.type == 'over_rarity_3_once':
            ret.guaranteed_type = 'rarity_3'
            ret.guaranteed_rarities = ['rarity_3', 'rarity_4', 'rarity_birthday']
    # 普通抽卡
    for rate in gacha.rarity_rates:
        if rate.lottery_type == 'normal':
            ret.rates[rate.rarity] = rate.rate / 100.0
    for rarity in GACHA_RATE_RARITIES:
        for card in ret.cards.get(rarity, []):
            card.rate = card.weight * ret.rates.get(rarity, 0) / ret.weights.get(rarity, 1)
    # 保底抽卡
    if ret.guaranteed_type:
        for rate in gacha.rarity_rates:
            if rate.lottery_type == 'normal':
                ret.guaranteed_rates[rate.rarity] = rate.rate / 100.0
        if ret.guaranteed_type in ('rarity_4', 'rarity_3'):
            ret.guaranteed_rates[ret.guaranteed_type] += ret.rates.get('rarity_2', 0)
            ret.guaranteed_rates['rarity_2'] = 0
        if ret.guaranteed_type in ('rarity_4',):
            ret.guaranteed_rates[ret.guaranteed_type] += ret.rates.get('rarity_3', 0)
            ret.guaranteed_rates['rarity_3'] = 0
        for rarity in GACHA_RATE_RARITIES:
            for card in ret.cards.get(rarity, []):
                card.guaranteed_rate = card.weight * ret.guaranteed_rates.get(rarity, 0) / ret.weights.get(rarity, 1)
    return ret

# ================================ 卡池状态与参数解析 ================================ #

def is_unreleased_gacha(gacha: Gacha, now: datetime = None) -> bool:
    """以目标区服卡池自身的开放时间判断未上线，开始时刻起不再显示提示。"""
    return gacha.start_at > (now if now is not None else datetime.now())


def needs_gacha_leak_notice(gacha: Gacha, now: datetime = None) -> bool:
    """距本服开池超过七天时显示未实装提示，恰好七天起隐藏。"""
    return gacha.start_at - (now if now is not None else datetime.now()) > timedelta(days=7)


async def parse_gacha_detail_query(ctx, args):
    """先定位单池，再解析名单条件，保留裸p2、年份等卡池一览参数。"""
    words = args.lower().split()
    if not words:
        return None
    selector = words.pop(0)
    if re.fullmatch(r'-?\d+', selector):
        pass
    elif selector.startswith('event'):
        if selector == 'event' and words:
            selector += words.pop(0)
    else:
        return None
    gacha = await parse_search_gacha_args(ctx, selector)
    if gacha is None:
        return None
    full = False
    exchange = False
    show_unowned = False
    page = None
    conditions = []
    for word in words:
        if word == 'full':
            assert_and_reply(not full, 'full参数重复')
            full = True
        elif word == '兑换':
            assert_and_reply(not exchange, '兑换参数重复')
            exchange = True
        elif word in ('缺卡', '未拥有', '未持有', 'unowned'):
            assert_and_reply(not show_unowned, '请只指定一次未持有筛选')
            show_unowned = True
        elif re.fullmatch(r'p-?\d+', word):
            assert_and_reply(page is None, '请只指定一个页码')
            page = int(word[1:])
            assert_and_reply(page >= 1, '页码从p1开始')
        else:
            conditions.append(word)
    # 完整页码标记才进入分页分支，查卡原有的p分等技能条件继续交给查卡解析。
    return GachaDetailQuery(gacha, full or exchange or show_unowned or bool(conditions) or page is not None,
                            ' '.join(conditions), page or 1, exchange, show_unowned)


# 解析查单个卡池指令参数
async def parse_search_gacha_args(ctx: SekaiHandlerContext, args: str) -> Optional[Gacha]:
    if not args:
        return None
    # event只在选择器开头生效，名单中的event123交给查卡条件解析。
    if args.startswith('event'):
        eid = (await parse_search_single_event_args(ctx, args[len('event'):].strip()))['id']
        g = await get_gacha_by_event_id(ctx, eid)
        assert_and_reply(g, f"未找到活动{ctx.region.upper()}-{eid}对应的卡池，请按卡池编号查询")
        return g
    index = None
    try:
        index = int(args)
    except ValueError:
        pass
    if index is not None:
        if index >= 0:
            gacha = await ctx.md.gachas.find_by_id(index)
            assert_and_reply(gacha, f"找不到卡池{ctx.region.upper()}-{index}")
            return gacha
        else:
            index = -index
            gachas = [
                g for g in sorted(await ctx.md.gachas.get(), key=lambda g: (g.start_at, g.id), reverse=True)
                if datetime.now() >= g.start_at
            ]
            assert_and_reply(index <= len(gachas), f"找不到最近的第{index}个卡池")
            return gachas[index - 1]
    return None

# 解析查多个卡池指令参数
async def parse_search_multiple_gacha_args(ctx: SekaiHandlerContext, args: str) -> Tuple[GachaFilter, str]:
    is_rerelease = False
    if '复刻' in args:
        args = args.replace('复刻', '').strip()
        is_rerelease = True
    
    is_recall = False
    if '回响' in args:
        args = args.replace('回响', '').strip()
        is_recall = True
    
    is_current = False
    if '当前' in args:
        args = args.replace('当前', '').strip()
        is_current = True
    
    is_leak = False
    if 'leak' in args:
        args = args.replace('leak', '').strip()
        is_leak = True

    year, args = extract_year(args)

    card_id = None
    match = re.match(r'card(\d+)', args)
    if match:
        card_id = int(match.group(1))
        args = args.replace(match.group(0), '').strip()

    page = None
    match = re.match(r'p(\d+)', args)
    if match:
        page = int(match.group(1))
        args = args.replace(match.group(0), '').strip()
    match = re.match(r'(\d+)页', args)
    if match:
        page = int(match.group(1))
        args = args.replace(match.group(0), '').strip()

    return GachaFilter(
        page=page,
        year=year,
        card_id=card_id,
        is_rerelease=is_rerelease,
        is_recall=is_recall,
        is_current=is_current,
        is_leak=is_leak,
    ), args.strip()
    
# 合成卡池一览图片
async def compose_gacha_list_image(ctx: SekaiHandlerContext, filter: GachaFilter = None):
    filter = filter or GachaFilter()
    now = datetime.now()
    gachas: List[Gacha] = []
    for gacha in await ctx.md.gachas.get():
        g: Gacha = gacha
        if filter.card_id and not find_by_predicate(g.cards, lambda c: c.id == filter.card_id):
            continue
        if filter.year and g.start_at.year != filter.year:
            continue
        if filter.is_rerelease and not g.name.startswith(RERELEASE_KEYWORDS):
            continue
        if filter.is_recall and not g.name.startswith(ECHO_KEYWORDS):
            continue
        if filter.is_current and not (g.start_at <= now <= g.end_at):
            continue
        if filter.is_leak and not is_unreleased_gacha(g, now):
            continue
        if not filter.is_leak and is_unreleased_gacha(g, now):
            continue

        gachas.append(g)
    assert_and_reply(gachas, "找到没有符合条件的卡池")
    gachas.sort(key=lambda g: g.start_at)

    page_size = GACHA_LIST_PAGE_SIZE_CFG.get()
    total_pages = math.ceil(len(gachas) / page_size)
    page = max(1, min(filter.page, total_pages)) if filter.page is not None else total_pages
    start_index = (page - 1) * page_size
    gachas = gachas[start_index:start_index+page_size]
    logos = await batch_gather(*[get_gacha_logo(ctx, g) for g in gachas])

    row_count = math.ceil(math.sqrt(len(gachas)))
    style1 = TextStyle(font=DEFAULT_HEAVY_FONT, size=10, color=(50, 50, 50))
    style2 = TextStyle(font=DEFAULT_FONT,       size=10, color=(70, 70, 70))

    with Canvas(bg=SEKAI_BLUE_BG).set_padding(BG_PADDING) as canvas:
        with VSplit().set_padding(0).set_sep(4).set_content_align('lt').set_item_align('lt'):
            TextBox(
                f"卡池按时间顺序排列，黄色为开放中卡池，当前为第 {page}/{total_pages} 页",
                TextStyle(font=DEFAULT_FONT, size=12, color=(0, 0, 100))
            ).set_bg(roundrect_bg(radius=4)).set_padding(4)
            with Grid(row_count=row_count, vertical=True).set_sep(8, 2).set_item_align('c').set_content_align('c'):
                for g, logo in zip(gachas, logos):
                    now = datetime.now()
                    bg_color = WIDGET_BG_COLOR_CFG.get()
                    if g.start_at <= now <= g.end_at:
                        bg_color = (255, 250, 220, 200)
                    elif now > g.end_at:
                        bg_color = (220, 220, 220, 200)
                    bg = roundrect_bg(bg_color, 5)
                    with HSplit().set_padding(4).set_sep(4).set_item_align('lt').set_content_align('lt').set_bg(bg):
                        with VSplit().set_padding(0).set_sep(2).set_item_align('lt').set_content_align('lt'):
                            ImageBox(logo, size=(None, 60))
                            TextBox(f"【{g.id}】{g.name}", style1, line_count=2, use_real_line_count=False).set_w(130)
                            TextBox(f"S {g.start_at.strftime('%Y-%m-%d %H:%M')}", style2)
                            TextBox(f"T {g.end_at.strftime('%Y-%m-%d %H:%M')}", style2)

    add_watermark(canvas)
    return await canvas.get_img()

# ================================ 招募消耗选项排版 ================================ #

async def draw_gacha_cost_options(ctx, behaviors, style, width):
    """把一种招募方式的支付选项按剩余宽度换行，保留原图标和字号。"""
    with VSplit().set_padding(0).set_sep(8).set_content_align('l').set_item_align('l') as container:
        options = []
        for behavior in behaviors:
            with HSplit().set_padding(0).set_sep(8).set_content_align('l').set_item_align('c') as option:
                if behavior.cost_type:
                    res = await get_res_icon(ctx, behavior.cost_type, behavior.cost_id)
                    ImageBox(res, size=(None, 48))
                    if 'paid' in behavior.cost_type:
                        TextBox('(付费)', style)
                    if behavior.cost_quantity > 1:
                        TextBox(f'x{behavior.cost_quantity}', style)
                else:
                    TextBox('免费', style)
            options.append(option)
        separator = TextBox(' / ', style)
        separator_width = separator._get_self_size()[0] + 16
        container.set_items([])
        rows = []
        current = []
        current_width = 0
        for option in options:
            option_width = option._get_self_size()[0]
            added = option_width + (separator_width if current else 0)
            if current and current_width + added > width:
                rows.append(current)
                current = []
                current_width = 0
                added = option_width
            current.append(option)
            current_width += added
        if current:
            rows.append(current)
        for row in rows:
            with HSplit().set_padding(0).set_sep(8).set_content_align('l').set_item_align('c') as line:
                for index, option in enumerate(row):
                    if index:
                        TextBox(' / ', style)
                    line.add_item(option)
    return container


async def get_gacha_content_width(ctx, gacha, label_style, style):
    """先按实际消耗适度拓宽，超过基础宽度20%的部分交给支付选项换行。"""
    behaviors = {}
    for behavior in gacha.behaviors:
        behaviors.setdefault(get_gacha_behavior_text(behavior), []).append(behavior)
    label_width = max((math.ceil(TextBox(text, label_style)._get_pil_font().getlength(text)) + 4
                       for text in behaviors), default=0)
    option_width = 0
    # 测量帧在页面画布创建前使用，里面的图标和文字不会混入正式布局。
    with Frame().set_padding(0):
        for options in behaviors.values():
            group = await draw_gacha_cost_options(ctx, options, style, 100000)
            option_width = max(option_width, group._get_self_size()[0])
    return min(720, max(600, label_width + option_width + 40))


async def draw_gacha_cost_grid(ctx, behaviors, label_style, style, width):
    """根据招募方式标签的实际宽度分配支付选项区域，避免撑宽整栏。"""
    with VSplit().set_padding(0).set_sep(8).set_content_align('l').set_item_align('l') as container:
        labels = [TextBox(text, label_style) for text in behaviors]
        # 控件测量会取整，标签列按字形实际步进宽度向上取整，防止末字被单行省略。
        label_width = max((max(label._get_self_size()[0],
                              math.ceil(label._get_pil_font().getlength(label.text)) + label.hpadding * 2)
                           for label in labels), default=0)
        # 外层消耗面板左右各有16px内边距，标签和选项之间留8px。
        option_width = max(1, width - 32 - label_width - 8)
        minimum_height = 48 if any(behavior.cost_type for options in behaviors.values() for behavior in options) else 0
        container.set_items([])
        for label, options in zip(labels, behaviors.values()):
            # 各行独立计算高度，只有需要换行的支付方式增加高度。
            with HSplit().set_padding(0).set_sep(8).set_content_align('l').set_item_align('l') as row:
                label.set_w(label_width)
                label._calc_w = label._calc_h = None
                row.add_item(label)
                await draw_gacha_cost_options(ctx, options, style, option_width)
            # 有资源图标的消耗区保持原48px基础行高，免费行也采用同一基线。
            if row._get_self_size()[1] < minimum_height:
                row.set_h(minimum_height)
                row._calc_w = row._calc_h = None


# 合成卡池详情图片
# ================================ 完整招募名单与筛选 ================================ #

def get_gacha_list_rarity(gacha):
    """按卡池概率表选择名单稀有度，生日池展示生日成员，其余池展示四星。"""
    return 'rarity_birthday' if any(rate.rarity == 'rarity_birthday' and rate.rate > 0
                                   for rate in gacha.rarity_rates) else 'rarity_4'


async def prepare_gacha_full_list(ctx, gacha, conditions='', *, exchange=False):
    """限定本池对应稀有度或兑换范围，复用查卡条件并在副本上添加筛选信息。"""
    rarity = 'rarity_4' if exchange else get_gacha_list_rarity(gacha)
    pool_ids = {card.id for card in gacha.cards}
    source = [dict(card) for card in await ctx.md.cards.get()
              if card['id'] in pool_ids and card['cardRarityType'] == rarity]
    if exchange:
        assert_and_reply(not raw_table(ctx.region, '_unavailable_tables'),
                         '暂时无法读取完整兑换范围，请稍后再试')
        entries = get_gacha_exchange_entries(gacha.raw, ctx.region)
        assert_and_reply(entries, '本卡池没有可查询的成员自选券')
        assert_and_reply(all(entry['complete'] for entry in entries), '暂时无法读取完整兑换范围，请稍后再试')
        exchange_ids = entries[0]['card_ids']
        assert_and_reply(all(entry['card_ids'] == exchange_ids for entry in entries),
                         '本卡池包含不同兑换范围的自选券，暂不支持合并查询')
        source = [dict(card) for card in await ctx.md.cards.get() if card['id'] in exchange_ids]
    selected, remaining = await search_multi_cards(ctx, conditions, cards=source, contain_leak=True)
    assert_and_reply(not remaining, f'无法识别的查卡条件：{remaining}')
    selection = bool(gacha.raw.get('wishSelectCount') or gacha.raw.get('rateChoiceGachaWishGroupId'))
    up_ids = ({row['cardId'] for row in gacha.raw.get('gachaDetails', [])
               if row.get('gachaDetailWishType') == 'fixed'} if selection
              else {card.id for card in gacha.cards if card.is_pickup})
    if exchange:
        up_ids = set()
    ordered = sorted(selected, key=lambda card: (
        card['id'] not in up_ids, -card['releaseAt'], -card['id']))
    return dict(pool_total=len(source), matched=len(ordered), conditions=conditions,
                ordered_ids=[card['id'] for card in ordered], up_ids=up_ids, exchange=exchange,
                rarity=rarity)


def choose_gacha_full_grid(count, budget, has_filter=False):
    """根据左栏高度和结果数量选择网格，小名单放大卡图，大名单按完整行分页。"""
    candidates = []
    for columns in (3, 4, 5, 6, 8):
        gap = 12 if columns <= 4 else 10 if columns == 5 else 8
        thumb = min(144, (632 - (columns - 1) * gap) // columns)
        rows = math.ceil(count / columns)
        overhead = 180 if has_filter else 148
        row_height = thumb + (31 if thumb >= 100 else 29)
        if overhead + rows * row_height <= budget + 60:
            occupied = min(count, columns) * thumb + (min(count, columns) - 1) * gap
            candidates.append((-thumb, -occupied, columns, thumb, gap))
    if candidates:
        _, _, columns, thumb, gap = min(candidates)
        return dict(columns=columns, thumb=thumb, gap=gap, capacity=max(1, count))
    overhead = 212 if has_filter else 180
    rows = max(1, round((budget - overhead) / 101))
    return dict(columns=8, thumb=72, gap=8, capacity=rows * 8)


async def draw_gacha_full_list(ctx, gacha, left_column, state, page):
    """完整名单替换右侧扩展，左侧原详情决定分页容量。"""
    with VSplit().set_padding(0).set_sep(16).set_content_align('lt').set_item_align('lt') as right:
        profile_height = 0
        if profile := state.get('personal_profile'):
            profile_card = await get_detailed_profile_card(ctx, profile, '')
            profile_card.set_w(680)
            profile_card._calc_w = profile_card._calc_h = None
            profile_height = profile_card._get_self_size()[1] + 16
        budget = max(250, left_column._get_self_size()[1] - profile_height) if profile_height else max(370, left_column._get_self_size()[1])
        grid = choose_gacha_full_grid(state['matched'], budget, bool(state['conditions']))
        pages = max(1, math.ceil(state['matched'] / grid['capacity']))
        assert_and_reply(1 <= page <= pages, f'该名单共{pages}页，请使用p1至p{pages}')
        start = (page - 1) * grid['capacity']
        card_ids = state['ordered_ids'][start:start + grid['capacity']]
        style = TextStyle(font=DEFAULT_FONT, size=20, color=(70, 70, 70))
        title_style = TextStyle(font=DEFAULT_BOLD_FONT, size=24, color=(50, 50, 50))
        small_style = TextStyle(font=DEFAULT_FONT, size=16, color=(70, 70, 70))
        id_style = TextStyle(font=DEFAULT_FONT, size=16 if grid['thumb'] >= 100 else 14, color=(70, 70, 70))
        up_style = id_style.replace(color=(50, 150, 50))
        rarity_name = '生日' if state['rarity'] == 'rarity_birthday' else '四星'
        with VSplit().set_w(680).set_padding(24).set_sep(12).set_content_align('lt').set_item_align('lt').set_bg(roundrect_bg()) as gallery:
            TextBox(f"{rarity_name}{'兑换范围' if state.get('exchange') else '招募范围'}", title_style)
            if state['conditions']:
                result_label = '筛选后未持有' if state.get('personal_profile') else '筛选后'
                TextBox(f"{'兑换范围' if state.get('exchange') else '本池'}共{state['pool_total']}张，{result_label}{state['matched']}张", style)
                words = []
                for word in state['conditions'].split():
                    character_id = get_cid_by_nickname(word)
                    words.append(await get_character_name_by_id(ctx, character_id) if character_id else word)
                TextBox('筛选：' + ' '.join(words), small_style)
            elif card_ids:
                count_label = f"未持有{state['matched']}张" if state.get('personal_profile') else f"共{state['pool_total']}张"
                TextBox(f"{count_label}，本页{start + 1}～{start + len(card_ids)}张", style)
            if not card_ids:
                empty_text = ('没有符合条件的未持有成员' if state['conditions'] else f'本范围{rarity_name}成员已全部持有') if state.get('personal_profile') else (f'没有符合条件的{rarity_name}成员' if state['conditions'] else f'本范围没有{rarity_name}成员')
                TextBox(empty_text, style)
            else:
                with Grid(col_count=grid['columns']).set_padding(0).set_sep(grid['gap'], 8).set_content_align('lt').set_item_align('lt'):
                    for card_id in card_ids:
                        thumbnail = await get_card_full_thumbnail(ctx, card_id)
                        with VSplit().set_padding(0).set_sep(3).set_content_align('c').set_item_align('c'):
                            with Frame().set_content_align('rt'):
                                ImageBox(thumbnail, size=(grid['thumb'], grid['thumb']), shadow=True)
                                supply = await get_card_supply_type(ctx, card_id)
                                if supply in ('term_limited', 'world_bloom_limited', 'collab_limited'):
                                    ImageBox(ctx.static_imgs.get('card/term_limited.png'), size=(int(grid['thumb'] * .6), None))
                                elif supply in ('colorful_festival_limited', 'bloom_festival_limited'):
                                    ImageBox(ctx.static_imgs.get('card/fes_limited.png'), size=(int(grid['thumb'] * .6), None))
                            with HSplit().set_sep(4).set_content_align('c').set_item_align('c'):
                                TextBox(str(card_id), id_style)
                                if card_id in state['up_ids']:
                                    TextBox('UP', up_style)
            if state['matched'] or state.get('personal_profile') is None:
                await draw_gacha_list_footer(ctx, gacha, state['conditions'], page, pages,
                                             exchange=state.get('exchange', False),
                                             show_unowned=state.get('personal_profile') is not None, width=632)
    return dict(page=page, pages=pages, grid=grid, shown=card_ids,
                left_size=left_column._get_self_size(), right_size=right._get_self_size())


def gacha_query_command(ctx, gacha, *, exchange=False, show_unowned=False, conditions='', page=None):
    """生成区服指令，仅具体的个人名单翻页携带账号选择和抓包模式。"""
    parts = [f'/{ctx.region}查卡池', str(gacha.id)]
    if exchange:
        parts.append('兑换')
    if show_unowned:
        parts.append('未持有')
    if conditions:
        parts.append(conditions)
    if page is not None:
        parts.append(f'p{page}')
    if page is not None and show_unowned and (uid_arg := getattr(ctx, 'uid_arg', None)):
        parts.append(str(uid_arg))
    if page is not None and show_unowned and (mode := getattr(ctx, 'data_mode_arg', None)):
        parts.append(f'抓包模式{mode}')
    return ' '.join(parts)


def draw_gacha_query_guide(rows, width):
    """在当前侧绘制独立引导卡，文字使用常规字体和中文引号。"""
    parent = VSplit.get_current_widget()
    background = None if getattr(parent, 'item_bg', None) is not None else roundrect_bg()
    style = TextStyle(font=DEFAULT_FONT, size=18, color=BLACK)
    with VSplit().set_w(width).set_padding((16, 8)).set_sep(6).set_content_align('l').set_item_align('l').set_bg(background) as guide:
        for text, counter_text in rows:
            with HSplit().set_sep(0).set_content_align('l').set_item_align('lb') as row:
                counter = TextBox(counter_text, style) if counter_text else None
                counter_width = counter._get_self_size()[0] + 2 if counter else 0
                if counter:
                    row.remove_item(counter)
                TextBox(text, style, use_real_line_count=True).set_w(width - 32 - counter_width)
                if counter:
                    row.add_item(counter)
    guide.userdata['gacha_query_guide'] = True
    return guide


async def draw_gacha_list_footer(ctx, gacha, conditions, page, pages, *, exchange=False, show_unowned=False, width=632):
    """在名单大卡底部展示筛选引导，多页时在右侧显示页码。"""
    command = gacha_query_command(ctx, gacha, exchange=exchange, conditions='<条件>')
    with VSplit().set_w(width).set_padding(0).set_sep(0) as footer:
        Spacer(w=0, h=4)
        with HSplit().set_w(width).set_padding(0).set_sep(16).set_item_align('c').set_content_align('l') as row:
            counter = None
            if pages > 1:
                counter = TextBox(f'{page} / {pages}', TextStyle(font=DEFAULT_FONT, size=16, color=(70, 70, 70)))
                counter.userdata['page_counter'] = True
                row.remove_item(counter)
            counter_width = counter._get_self_size()[0] + 16 if counter else 0
            TextBox(f'发送“{command}”筛选名单', TextStyle(font=DEFAULT_FONT, size=18, color=BLACK),
                    use_real_line_count=True).set_w(width - counter_width)
            if counter:
                row.add_item(counter)
    return footer


async def _compose_gacha_detail_image(ctx: SekaiHandlerContext, gacha: Gacha, full_data=None, page=1):
    logo, banner, card_img = _GACHA_RENDER_DATA.get()[1]['_base_images']
    bg = ImageBg(card_img) if card_img is not None and card_img != UNKNOWN_IMG else SEKAI_BLUE_BG
    weight_info = await weight_info_for_display(ctx, gacha)
    pickup_rate = 0.0
    pickup_cards: List[GachaCardWeightInfo] = []
    card_ids = set()
    for cards in weight_info.cards.values():
        for card in cards:
            if card.is_pickup:
                card_ids.add(card.id)
                pickup_rate += card.rate
                pickup_cards.append(card)
    # 单次招募可能只有当期卡而没有十连保底，概率行仍会读取该值。
    guaranteed_pickup_rate = 0.0
    if weight_info.guaranteed_type:
        guaranteed_pickup_cards: List[GachaCardWeightInfo] = []
        for cards in weight_info.cards.values():
            for card in cards:
                if card.is_pickup:
                    guaranteed_pickup_rate += card.guaranteed_rate
                    guaranteed_pickup_cards.append(card)
    card_ids = list(card_ids)
    thumbs = await batch_gather(*[get_card_full_thumbnail(ctx, card_id) for card_id in card_ids])
    card_thumbs: Dict[int, Image.Image] = {id: thumb for id, thumb in zip(card_ids, thumbs)}
    raw_preview = raw_gacha(ctx.region, gacha.id)
    try:
        inline_extension = single_column(raw_preview, ctx.region)
    except GachaExtensionDataError as error:
        _mark_gacha_extension_unavailable(ctx, gacha, _GACHA_RENDER_DATA.get()[1], error)
        inline_extension = True
    selection_examples = bool(raw_preview.get('wishSelectCount')) and (not raw_preview.get('wishFixedSelectCount'))
    title_style = TextStyle(font=DEFAULT_BOLD_FONT, size=24, color=BLACK)
    label_style = TextStyle(font=DEFAULT_BOLD_FONT, size=24, color=(50, 50, 50))
    text_style = TextStyle(font=DEFAULT_FONT, size=24, color=(70, 70, 70))
    small_style = TextStyle(font=DEFAULT_FONT, size=12, color=(70, 70, 70))
    tip_style = TextStyle(font=DEFAULT_FONT, size=18, color=(0, 0, 0))
    content_width = await get_gacha_content_width(ctx, gacha, label_style, text_style)
    with Canvas(bg=bg).set_padding(BG_PADDING) as canvas:
        with HSplit().set_sep(16).set_content_align('lt').set_item_align('lt'):
            w = content_width
            with VSplit().set_padding(8).set_sep(8).set_content_align('c').set_item_align('c').set_item_bg(roundrect_bg()).set_bg(roundrect_bg()) as preview_base_column:
                with HSplit().set_padding(8).set_sep(32).set_content_align('c').set_item_align('c').set_omit_parent_bg(True):
                    if logo:
                        ImageBox(logo, size=(None, 100))
                    if banner:
                        ImageBox(banner, size=(None, 100))
                TextBox(gacha.name, title_style, use_real_line_count=True).set_w(w).set_padding(16).set_content_align('c')
                with HSplit().set_padding(16).set_sep(8).set_content_align('c').set_item_align('c'):
                    TextBox('ID', label_style)
                    TextBox(f'{gacha.id} ({ctx.region.upper()})', text_style)
                    Spacer(w=24)
                    TextBox('类型', label_style)
                    TextBox(GACHA_TYPE_NAMES[gacha.type], text_style)
                    if gacha.ceilitem_id:
                        Spacer(w=24)
                        TextBox('交换物品', label_style)
                        ImageBox(await get_res_icon(ctx, 'gacha_item', gacha.ceilitem_id), size=(None, 30))
                with VSplit().set_padding(16).set_sep(8).set_content_align('c').set_item_align('c'):
                    with HSplit().set_padding(0).set_sep(8).set_content_align('c').set_item_align('c'):
                        TextBox('开始时间', label_style)
                        TextBox(gacha.start_at.strftime('%Y-%m-%d %H:%M'), text_style)
                    with HSplit().set_padding(0).set_sep(8).set_content_align('c').set_item_align('c'):
                        TextBox('结束时间', label_style)
                        TextBox('以账号开放条件为准' if raw_preview['gachaType'] in ('sureturn', 'subeginner') and gacha.end_at.year >= 2099 else gacha.end_at.strftime('%Y-%m-%d %H:%M'), text_style)
                    with HSplit().set_padding(0).set_sep(8).set_content_align('c').set_item_align('c'):
                        if is_unreleased_gacha(gacha):
                            TextBox('距离开始还有', label_style)
                            TextBox(get_readable_timedelta(gacha.start_at - datetime.now()), text_style)
                        elif gacha.end_at >= datetime.now():
                            TextBox('距离结束还有', label_style)
                            TextBox(get_readable_timedelta(gacha.end_at - datetime.now()), text_style)
                        else:
                            TextBox('卡池已结束', label_style)
                with VSplit().set_padding(16).set_sep(16).set_content_align('c').set_item_align('c'):
                    behaviors: Dict[str, List[GachaBehavior]] = {}
                    for behavior in gacha.behaviors:
                        text = get_gacha_behavior_text(behavior)
                        behaviors.setdefault(text, []).append(behavior)
                    await draw_gacha_cost_grid(ctx, behaviors, label_style, text_style, w)
                if pickup_cards:
                    with HSplit().set_padding(16).set_sep(16).set_content_align('c').set_item_align('c'):
                        TextBox('示例卡片' if selection_examples else '当期卡片', label_style)
                        with Grid(col_count=min(5, len(pickup_cards))).set_padding(0).set_sep(8, 8).set_content_align('c').set_item_align('c'):
                            card_size = 80
                            for card in pickup_cards:
                                with VSplit().set_padding(0).set_sep(1).set_content_align('c').set_item_align('c'):
                                    with Frame().set_content_align('rt'):
                                        ImageBox(card_thumbs[card.id], size=(card_size, card_size), shadow=True)
                                        supply = await get_card_supply_type(ctx, card.id)
                                        if supply in ('term_limited', 'world_bloom_limited', 'collab_limited'):
                                            ImageBox(ctx.static_imgs.get('card/term_limited.png'), size=(int(card_size * 0.6), None))
                                        elif supply in ('colorful_festival_limited', 'bloom_festival_limited'):
                                            ImageBox(ctx.static_imgs.get('card/fes_limited.png'), size=(int(card_size * 0.6), None))
                                    TextBox(str(card.id) if selection_examples else f'{card.id} ({get_float_str(card.rate * 100, 4)}%)', small_style)
                with VSplit().set_padding(16).set_sep(8).set_content_align('c').set_item_align('c'):
                    with Grid(col_count=2).set_padding(0).set_sep(8, 8).set_content_align('l').set_item_align('l'):
                        for rarity in ['pickup'] + GACHA_RATE_RARITIES:
                            cards = rarity == 'pickup' and pickup_cards or weight_info.cards.get(rarity, [])
                            if not cards or (rarity == 'pickup' and selection_examples):
                                continue
                            rarity_img = None if rarity == 'pickup' else await get_rarity_img(ctx, rarity)
                            rate = pickup_rate if rarity == 'pickup' else weight_info.rates.get(rarity, 0)
                            guaranteed_rate = guaranteed_pickup_rate if rarity == 'pickup' else weight_info.guaranteed_rates.get(rarity, 0)
                            with HSplit().set_padding(0).set_sep(8).set_content_align('l').set_item_align('l'):
                                if rarity_img:
                                    ImageBox(rarity_img, size=(None, 24))
                                else:
                                    TextBox('当期', label_style)
                                TextBox(f'({len(cards)})', text_style)
                            rate_text = f'{get_float_str(rate * 100, 4)}%'
                            if weight_info.guaranteed_type and guaranteed_rate > 0:
                                rate_text += f' / {get_float_str(guaranteed_rate * 100, 4)}% (保底)'
                            TextBox(rate_text, text_style)
                preview_base_width = max((item._get_self_size()[0] for item in preview_base_column.items))
                if inline_extension:
                    await draw_extension(ctx, gacha, inline=True, inline_width=preview_base_width)
                if full_data is None:
                    command = gacha_query_command(ctx, gacha)
                    rarity_name = '生日' if get_gacha_list_rarity(gacha) == 'rarity_birthday' else '四星'
                    draw_gacha_query_guide([(f'发送“{command} full”查看{rarity_name}招募名单', ''),
                                            (f'发送“{command} <条件>”筛选名单', '')], preview_base_width)
                if needs_gacha_leak_notice(gacha):
                    TextBox(LEAK_NOTICE, TextStyle(font=DEFAULT_FONT, size=18, color=(200, 75, 75)), use_real_line_count=True).set_w(preview_base_width).set_padding((16, 8)).set_content_align('l').set_omit_parent_bg(True)
            if full_data is not None:
                await draw_gacha_full_list(ctx, gacha, preview_base_column, full_data, page)
            elif not inline_extension:
                sidebar = await draw_extension(ctx, gacha)
                if sidebar is not None:
                    balance_gacha_columns(preview_base_column, sidebar)
    add_watermark(canvas)
    return await canvas.get_img()


# 模拟抽卡
async def spin_gacha(ctx: SekaiHandlerContext, gacha: Gacha, count: int) -> List[GachaSpinResult]:
    assert_and_reply(count == 1 or count % 10 == 0, "抽卡次数必须为1或10的倍数")
    info = await get_gacha_weight_info(ctx, gacha)

    all_cards: List[GachaCardWeightInfo] = []
    all_guaranteed_cards: List[GachaCardWeightInfo] = []
    for rarity, cards in info.cards.items():
        all_cards.extend(cards)
        if rarity in info.guaranteed_rarities:
            all_guaranteed_cards.extend(cards)
    all_weights = [c.rate for c in all_cards]
    all_guaranteed_weights = [c.guaranteed_rate for c in all_guaranteed_cards]

    ret: List[GachaSpinResult] = []
    guarantee = True
    for i in range(1, count + 1):
        cards, weights = all_cards, all_weights
        if i % 1 == 0:
            guarantee = True
        if i % 10 == 0 and guarantee and info.guaranteed_type:
            cards, weights = all_guaranteed_cards, all_guaranteed_weights
        result = random.choices(cards, weights=weights, k=1)[0]
        ret.append(GachaSpinResult(
            id=result.id,
            rarity=result.rarity,
            is_pickup=result.is_pickup,
        ))
        if result.rarity in info.guaranteed_rarities:
            guarantee = False
    return ret

# 合成抽卡结果图片
async def compose_gacha_spin_image(ctx: SekaiHandlerContext, gacha: Gacha, cards: List[GachaSpinResult]):
    count = len(cards)
    grid_items: List[Union[int, str]] = []
    if count <= 10:
        grid_items = [c.id for c in cards]
    else:
        rarity_cards: Dict[str, List[GachaSpinResult]] = {}
        for c in cards:
            rarity = c.rarity if not c.is_pickup else 'pickup'
            rarity_cards.setdefault(rarity, []).append(c)
        RARITY_ORDER = ['pickup', 'rarity_birthday', 'rarity_4', 'rarity_3', 'rarity_2', 'rarity_1']
        for rarity in RARITY_ORDER:
            cards = rarity_cards.get(rarity, [])
            if not cards:
                continue
            show_num = config.get(f'gacha.spin_result_show_num.{rarity}', 100)
            for c in cards[:show_num]:
                grid_items.append(c.id)
            if len(cards) > show_num:
                grid_items.append(f"...等\n共{len(cards)}张\n{GACHA_RARE_NAMES[rarity]}")
    
    logo_img = await get_gacha_logo(ctx, gacha)
    spin_text = "模拟单抽结果" if count == 1 else f"模拟{count}连结果"

    card_ids = set()
    for i in grid_items:
        if isinstance(i, int):
            card_ids.add(i)
    card_ids = list(card_ids)
    thumbs = await batch_gather(*[get_card_full_thumbnail(ctx, card_id) for card_id in card_ids])
    card_thumbs: Dict[int, Image.Image] = { id: thumb for id, thumb in zip(card_ids, thumbs) }

    # 绘图
    style1 = TextStyle(font=DEFAULT_BOLD_FONT, size=32, color=(75, 75, 75))
    style2 = TextStyle(font=DEFAULT_FONT, size=20, color=(75, 75, 75))
    thumb_size, sep = 100, 16
    if count <= 10: 
        thumb_size *= 2
        sep *= 2
    col_num = min(5 if count <= 10 else 10, len(grid_items))
    with Canvas(bg=SEKAI_BLUE_BG).set_padding(BG_PADDING) as canvas:
        with VSplit().set_padding(32).set_sep(24).set_content_align('lt').set_item_align('lt').set_bg(roundrect_bg()):
            with HSplit().set_padding(0).set_sep(32).set_content_align('l').set_item_align('l').set_omit_parent_bg(True):
                ImageBox(logo_img, size=(None, 100))
                TextBox(f"【{ctx.region.upper()}-{gacha.id}】{gacha.name}\n{spin_text}", style1, use_real_line_count=True)
            with Grid(col_count=col_num).set_padding(0).set_sep(sep, sep).set_content_align('c').set_item_align('c'):
                for item in grid_items:
                    if isinstance(item, int):
                        ImageBox(card_thumbs[item], size=(thumb_size, thumb_size), shadow=True)
                    else:
                        TextBox(item, style2, use_real_line_count=True) \
                            .set_size((thumb_size, thumb_size)).set_content_align('c').set_bg(roundrect_bg())

    add_watermark(canvas)
    return await canvas.get_img()

# 获取卡牌对应卡池
async def get_gacha_by_card_id(ctx: SekaiHandlerContext, cid: int) -> Optional[Gacha]:
    card = await ctx.md.cards.find_by_id(cid)
    release_time = datetime.fromtimestamp(card['releaseAt'] / 1000)
    for g in await ctx.md.gachas.get():
        if g.start_at <= release_time <= g.end_at:
            if find_by_predicate(g.cards, lambda x: x.id == cid and x.is_pickup):
                return g
    return None

async def get_gacha_by_event_id(ctx: SekaiHandlerContext, eid: int) -> Optional[Gacha]:
    """通过活动卡定位卡池，无法唯一确定时返回None。

    无活动卡的JP活动按完整主池名与活动展示起止时间严格匹配。
    """
    event_card_ids = sorted([item['cardId'] for item in await ctx.md.event_cards.find_by('eventId', eid, mode='all')])
    if event_card_ids:
        # 使用第三张卡，跳过fes卡。
        cid = event_card_ids[min(2, len(event_card_ids)-1)]
        return await get_gacha_by_card_id(ctx, cid)
    event = await ctx.md.events.find_by_id(eid)
    if not event or ctx.region != 'jp':
        return None
    start_at = event.get('eventOnlyComponentDisplayStartAt')
    end_at = event.get('eventOnlyComponentDisplayEndAt')
    if not event.get('name') or not start_at or not end_at:
        return None
    # 展示时间包含活动正式开场前的卡池开放阶段；完整名称排除奖励池和新手池。
    name = event['name'] + 'ガチャ'
    matches = [gacha for gacha in await ctx.md.gachas.get()
               if gacha.name == name and gacha.raw.get('startAt') == start_at
               and gacha.raw.get('endAt') == end_at]
    return matches[0] if len(matches) == 1 else None

# 合成抽卡记录图片
async def compose_gacha_record_image(ctx: SekaiHandlerContext, qid: int, spec_gids: list[int] | None = None):
    profile, err_msg = await get_detailed_profile(
        ctx, qid, raise_exc=True,
        filter=get_detailed_profile_card_filter('userCards', 'userGachas'),
    )

    # 数据获取
    assert_and_reply(profile.get('userGachas'), "没有找到抽卡记录，可能是最近没有抽过卡，或者Suite数据源未提供userGachas字段")
    ugachas = profile.get('userGachas', [])

    # 统计信息
    with ProfileTimer('gacha_record.records'):
        records: dict[int, dict] = {}
        for ug in ugachas:
            gacha_id, behavior_id = ug['gachaId'], ug['gachaBehaviorId']
            count, last_spin_at = ug.get('count', 0), ug.get('lastSpinAt')

            if not count:
                continue
            if spec_gids and gacha_id not in spec_gids:
                continue
            gacha: Gacha = await ctx.md.gachas.find_by_id(gacha_id)
            if not gacha:
                logger.warning(f"找不到卡池{ctx.region.upper()}-{gacha_id}，跳过该抽卡记录")
                continue

            records.setdefault(gacha_id, {
                'gacha': gacha,
                'name': gacha.name,
                'start_at': gacha.start_at,
                'end_at': gacha.end_at,
                'too_long': (gacha.end_at - gacha.start_at).days > 60,
                'behaviors': {},
            })
            if last_spin_at:
                last_spin_at = datetime.fromtimestamp(last_spin_at // 1000)
                records[gacha_id]['last_spin_at'] = max(records[gacha_id].get('last_spin_at', datetime.min), last_spin_at)

            behavior: GachaBehavior = find_by_predicate(gacha.behaviors, lambda b: b.id == behavior_id)
            if not behavior:
                logger.warning(f"找不到卡池{ctx.region.upper()}-{gacha_id}的抽卡行为{behavior_id}，跳过该抽卡记录")
                continue

            behavior_text = get_gacha_behavior_text(behavior)
            if behavior_text not in records[gacha_id]['behaviors']:
                records[gacha_id]['behaviors'][behavior_text] = ({
                    'total': 0,
                    'costs': {},
                })

            records[gacha_id]['behaviors'][behavior_text]['total'] += count
            if behavior.cost_type:
                res_key = (behavior.cost_type, behavior.cost_id)
                if res_key not in records[gacha_id]['behaviors'][behavior_text]['costs']:
                    res_icon = await get_res_icon(ctx, behavior.cost_type, behavior.cost_id)
                    res_text = "(付费)" if "paid" in behavior.cost_type else None
                    records[gacha_id]['behaviors'][behavior_text]['costs'][res_key] = {
                        'res_icon': res_icon,
                        'res_text': res_text,
                        'quantity': 0,
                    }
                records[gacha_id]['behaviors'][behavior_text]['costs'][res_key]['quantity'] += behavior.cost_quantity * count

    # 查找卡池可能的NEW卡
    with ProfileTimer('gacha_record.cards'):
        # 处理每个卡池的cards字典加速查找
        gcards_dict: dict[int, dict[int, GachaCard]] = {}
        for gid, gdata in records.items():
            if gdata['too_long']:
                gcards_dict[gid] = {}
                continue
            gacha: Gacha = gdata['gacha']
            gcards_dict[gid] = { c.id: c for c in gacha.cards }

        # 查找每个卡牌的可能卡池
        ucards = profile.get('userCards', [])
        ucard_possible_gacha_info: dict[int, list[dict]] = {}
        ucard_create_at: dict[int, datetime] = {}
        for uc in ucards:
            card = await ctx.md.cards.find_by_id(uc['cardId'])
            if not card:
                continue
            if card['cardRarityType'] in ('rarity_1', 'rarity_2'):
                continue

            cid = uc['cardId']
            created_at = datetime.fromtimestamp(uc['createdAt'] // 1000)
            ucard_create_at[cid] = created_at
            for rec in records.values():
                start_at = rec['start_at']
                end_at = rec.get('last_spin_at', rec['end_at'])
                if created_at < start_at or created_at > end_at:
                    continue
                gid = rec['gacha'].id
                if gcard := gcards_dict[gid].get(cid):
                    # 估计一个卡牌属于该卡池的可能性
                    weight = 0
                    if created_at == end_at: # 抽卡时间等于最后抽卡时间，可以肯定是
                        weight += 1e18
                    elif gcard.is_pickup: # 当期UP卡，可能性极大
                        weight += 1e9
                    else:   # 优先选择时间较短的一个卡池
                        weight += -(end_at - start_at).total_seconds()
                    ucard_possible_gacha_info.setdefault(cid, []).append((gid, weight))

        # 选择可能性最大的卡池作为NEW卡来源
        for cid, info in ucard_possible_gacha_info.items():
            if not info:
                continue
            info.sort(key=lambda x: x[1], reverse=True)
            gid, weight = info[0]
            determined = False
            if len(info) == 1 or weight > info[1][1] * 1e6:
                determined = True
            records[gid].setdefault('cards', []).append((cid, determined, ucard_create_at[cid]))

    # 清理记录
    for gid in list(records.keys()):
        gdata = records[gid]
        cards = gdata.get('cards')
        for btext in list(gdata['behaviors'].keys()):
            bdata = gdata['behaviors'][btext]
            # 删除次数为0的
            if bdata['total'] == 0: 
                del gdata['behaviors'][btext]
                continue
            # 如果没有new卡，删除免费抽卡
            if not cards and not any(cost['quantity'] > 0 for cost in bdata['costs'].values()):  
                del gdata['behaviors'][btext]
                continue
        # 删除没有抽卡行为且没有new卡的池子
        if not cards and not any(bdata['total'] > 0 for bdata in gdata['behaviors'].values()):
            del records[gid]
            continue

    assert_and_reply(records, "没有找到对应的抽卡记录")

    MAX_DRAW_COUNT = 50
    records = sorted([(gid, gdata) for gid, gdata in records.items() ], key=lambda x: x[1]['end_at'], reverse=True)
    hide_num = max(0, len(records) - MAX_DRAW_COUNT)
    records = records[:MAX_DRAW_COUNT]

    # 获取图片资源
    card_ids = set()
    for gid, gdata in records:
        card_ids.update([cid for cid, _, _ in gdata.get('cards', [])])
    card_ids = list(card_ids)
    card_thumbs = await batch_gather(*[get_card_full_thumbnail(ctx, cid) for cid in card_ids])
    card_thumbs = { cid: thumb for cid, thumb in zip(card_ids, card_thumbs) }
    
    gacha_ids = [gid for gid, _ in records]
    gacha_logos = await batch_gather(*[get_gacha_logo(ctx, gid) for gid in gacha_ids])
    gacha_logos = { gid: banner for gid, banner in zip(gacha_ids, gacha_logos) }

    style1 = TextStyle(font=DEFAULT_BOLD_FONT, size=18, color=(0, 0, 0))
    style2 = TextStyle(font=DEFAULT_FONT, size=16, color=(50, 50, 50))
    style3 = TextStyle(font=DEFAULT_FONT, size=16, color=(100, 0, 0))

    with Canvas(bg=SEKAI_BLUE_BG).set_padding(BG_PADDING) as canvas:
        with VSplit().set_content_align('lt').set_item_align('lt').set_sep(16):
            await get_detailed_profile_card(ctx, profile, err_msg)

            with VSplit().set_content_align('l').set_item_align('l').set_sep(16).set_item_bg(roundrect_bg()):
                msg = "上传时进行增量更新，未上传的记录将丢失\n"
                msg += "NEW卡通过抽卡时间与获得时间推测，可能不准确\n"
                msg += "未出NEW的免费抽卡(月卡单抽)不会显示"
                TextBox(msg, style1, use_real_line_count=True).set_padding(12)

                for gid, gdata in records:
                    with VSplit().set_content_align('lt').set_item_align('lt').set_sep(8).set_padding(16):
                        # 卡池信息
                        with HSplit().set_content_align('l').set_item_align('l').set_sep(8):
                            ImageBox(gacha_logos.get(gid), size=(None, 80))
                            with VSplit().set_content_align('l').set_item_align('l').set_sep(4):
                                TextBox(f"【{gid}】{gdata['name']}", style1, line_count=2).set_w(300)
                                TextBox(f"S {gdata['start_at'].strftime('%Y-%m-%d %H:%M')}", style2)
                                TextBox(f"T {gdata['end_at'].strftime('%Y-%m-%d %H:%M')}", style2)
                        # 抽卡记录
                        for btext, bdata in gdata['behaviors'].items():
                            with VSplit().set_content_align('l').set_item_align('l').set_sep(8).set_padding(6).set_bg(roundrect_bg()):
                                with HSplit().set_content_align('l').set_item_align('l').set_sep(4):
                                    TextBox(f"{btext} 共{bdata['total']}次  ", style2)
                                    for _, cost in bdata['costs'].items():
                                        ImageBox(cost['res_icon'], size=(None, 30))
                                        if cost['res_text']:
                                            TextBox(cost['res_text'], style2)
                                        TextBox(f"x{cost['quantity']} ", style2)
                        # NEW卡
                        if cards := gdata.get('cards', []):
                            cards.sort(key=lambda x: x[2])  # 按获得时间排序
                            if len(cards) <= 7:
                                max_col_count = 7
                                size, sep = 64, 6
                            else:
                                max_col_count = 12
                                size, sep = 40, 4
                            with Grid(col_count=min(len(cards), max_col_count)).set_content_align('l').set_item_align('l') \
                                .set_sep(sep, sep).set_padding(4):
                                for cid, determined, _ in cards:
                                    with Frame().set_content_align('rt'):
                                        ImageBox(card_thumbs[cid], size=(size, size), shadow=True)
                                        if not determined:
                                            qs = int(size * 0.3)
                                            TextBox("?", TextStyle(DEFAULT_BOLD_FONT, int(qs * 0.8), BLACK)).set_size((qs, qs)) \
                                                .set_bg(RoundRectBg((255, 255, 255, 200), radius=qs // 2)).set_content_align('c')
                        if gdata['too_long']:
                            TextBox("⚠️ 由于该卡池开放时间较长，不进行不准确的NEW卡推测", style3, use_real_line_count=True).set_padding(4)

                if hide_num:
                    TextBox(f"{hide_num}条抽卡记录已隐藏，可指定卡池ID查看", style2, use_real_line_count=True).set_padding(8)

    add_watermark(canvas)
    return await canvas.get_img()


# ======================= 指令处理 ======================= #
        
# 查卡池
pjsk_gacha = SekaiCmdHandler([
    "/pjsk gacha", "/卡池列表", "/卡池一览", "/卡池", "/查卡池", 
])
pjsk_gacha.check_cdrate(cd).check_wblist(gbl)
@pjsk_gacha.handle()
async def _(ctx: SekaiHandlerContext):
    args = ctx.get_args().strip()
    
    # 查单卡池
    query = await parse_gacha_detail_query(ctx, args)
    if query:
        started = time.perf_counter()
        trace_id = f'{ctx.region}/{query.gacha.id}/{time.monotonic_ns()}'
        image = await compose_gacha_detail_image(
            ctx, query.gacha, full=query.full, conditions=query.conditions, page=query.page,
            exchange=query.exchange, show_unowned=query.show_unowned, trace_id=trace_id)
        rendered = time.perf_counter()
        message = await get_image_cq(image, low_quality=True)
        encoded = time.perf_counter()
        result = await ctx.asend_reply_msg(message)
        logger.info(f'卡池发送耗时 {trace_id}: '
                    f'准备及绘图={rendered-started:.3f}s 编码={encoded-rendered:.3f}s '
                    f'发送={time.perf_counter()-encoded:.3f}s')
        return result
    
    # 查多个卡池
    filter, args = await parse_search_multiple_gacha_args(ctx, args)
    assert_and_reply(not args, f"""
查卡池参数错误: "{args}"
{SINGLE_GACHA_HELP}
{MULTI_GACHA_HELP}
""".strip())

    return await ctx.asend_reply_msg(await get_image_cq(
        await compose_gacha_list_image(ctx, filter),
        low_quality=True,
    ))


# 抽卡记录
pjsk_gacha_record = SekaiCmdHandler([
    "/pjsk gacha record", "/抽卡记录", "/抽卡历史",
])
pjsk_gacha_record.check_cdrate(cd).check_wblist(gbl)
@pjsk_gacha_record.handle()
async def _(ctx: SekaiHandlerContext):
    args = ctx.get_args().strip()
    spec_gids: list[int] | None = None
    if args:
        spec_gids = []
        for part in args.split():
            try:
                gid = int(part)
            except ValueError:
                raise ReplyException(f"卡池ID参数错误: {part}")
            assert_and_reply(await ctx.md.gachas.find_by_id(gid), f"找不到卡池{ctx.region.upper()}-{gid}")
            spec_gids.append(gid)

    return await ctx.asend_reply_msg(await get_image_cq(
        await compose_gacha_record_image(ctx, ctx.user_id, spec_gids),
        low_quality=True,
    ))
# ================================ 特殊卡池数据与绘图 ================================ #
# 普通卡池仍走原版单栏；扩展只读取当前请求需要的表，不使用历史样本编号或时钟。
from collections import defaultdict
import collections
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from pathlib import Path
import re
from zoneinfo import ZoneInfo
from .costume_crop import fixed_bounds, resolve_costume_pair

LEAK_NOTICE = '内容仅供参考，以正式实装为准'
JST = ZoneInfo('Asia/Tokyo')
_GACHA_RENDER_DATA = ContextVar('gacha_render_data')
_GACHA_PREPARE_LIMIT = ASSET_RESOURCE_LIMIT


async def _gacha_gather(*coroutines):
    """保序准备独立资源，失败或取消时收取所有子任务的结果。"""
    tasks = [asyncio.create_task(coroutine) for coroutine in coroutines]
    try:
        return await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def get_gacha_asset_image(ctx, path, **options):
    """在线程池读取和解码卡池图片，明确缺图仅缓存 60 秒。"""
    token = None
    if ASSET_REQUEST_LIMIT.get() is None:
        token = ASSET_REQUEST_LIMIT.set(_GACHA_PREPARE_LIMIT)
    try:
        return await ctx.rip.img(path, decode_in_pool=True, cache_missing_secs=60, **options)
    finally:
        if token is not None:
            ASSET_REQUEST_LIMIT.reset(token)


async def _prepare_gacha_base_images(ctx, gacha):
    """准备独立的基础图片，返回 Logo、Banner 和首张 UP 卡背景。"""
    async def background():
        identifier = next((card.id for card in gacha.cards if card.is_pickup), None)
        if identifier is None:
            return None
        card = await ctx.md.cards.find_by_id(identifier)
        return await get_card_image(ctx, identifier, after_training=has_after_training(card),
                                    allow_error=True, decode_in_pool=True)

    return await _gacha_gather(get_gacha_logo(ctx, gacha, default=None),
                               get_gacha_banner(ctx, gacha, default=None), background())


class GachaExtensionDataError(Exception):
    """卡池扩展缺少必要的关联记录，调用方可降级展示基础详情。"""

    def __init__(self, table, key):
        self.table = table
        super().__init__(f'{table} 缺少有效记录：{key}')


def _mark_gacha_extension_unavailable(ctx, gacha, tables, error):
    """修改本次请求的不可用表列表，并记录卡池及缺失记录。"""
    unavailable = tables.setdefault('_unavailable_tables', [])
    if error.table not in unavailable:
        unavailable.append(error.table)
    logger.warning(f'卡池扩展关联数据不完整：{ctx.region}/{gacha.id}：{error}')


def raw_table(region, name):
    """只访问本次已准备的关联数据；各查询通过ContextVar隔离。"""
    context, tables = _GACHA_RENDER_DATA.get()
    if context.region != region:
        raise ValueError('特殊卡池数据区服不一致')
    return tables.get(name, [])


def raw_gacha(region, gacha_id):
    """保留原主表记录，避免把映射后省掉的机制字段重新猜回来。"""
    return next(row for row in raw_table(region, 'gachas') if row['id'] == gacha_id)


def gacha_stage_rows(raw, tables):
    """按页签顺序读取整套阶段，缺少阶段组或子池时抛出 GachaExtensionDataError。"""
    if raw['gachaType'] not in ('sureturn', 'subeginner', 'sunormal'):
        return [raw]
    children = next((row['childGachaIds'] for row in tables.get('gachaTabs', [])
                     if raw['id'] in row.get('childGachaIds', [])), None)
    if not children:
        raise GachaExtensionDataError('gachaTabs', raw['id'])
    rows = {row['id']: row for row in tables.get('gachas', [raw])}
    for gacha_id in children:
        if gacha_id not in rows:
            raise GachaExtensionDataError('gachas', gacha_id)
    return [rows[gacha_id] for gacha_id in children]


def _get_gacha_freebie_group(raw, rows):
    """读取本池随机附赠组，缺组或权重无效时抛出 GachaExtensionDataError。"""
    group_id = raw['gachaFreebieGroupId']
    group = [row for row in rows if row['groupId'] == group_id]
    if (not group or any(not isinstance(row.get('weight'), (int, float)) or row['weight'] < 0 for row in group)
            or sum(row['weight'] for row in group) <= 0):
        raise GachaExtensionDataError('gachaFreebieGroups', group_id)
    return group


def _validate_gacha_render_data(raw, tables):
    """校验当前卡池使用的阶段、附赠组和奖励盒，返回实际引用的奖励盒键。"""
    stages = gacha_stage_rows(raw, tables)
    extra_ids = {row['gachaExtraId'] for stage in stages for row in stage.get('gachaBehaviors', [])
                 if row.get('gachaExtraId')}
    extras = {row['id']: row for row in tables.get('gachaExtras', []) if row['id'] in extra_ids}
    selected_boxes = set()
    for extra_id in extra_ids:
        extra = extras.get(extra_id)
        if extra is None or not extra.get('resourceBoxId'):
            raise GachaExtensionDataError('gachaExtras', extra_id)
        selected_boxes.add(('gacha_extra', extra['resourceBoxId']))
    for field, name, purpose in (
        ('gachaFreebieGroupId', 'gachaFreebieGroups', 'gacha_freebie_group'),
        ('gachaBonusItemReceivableRewardGroupId', 'gachaBonusItemReceivableRewards', 'gacha_bonus_item_receivable_reward'),
        ('rateChoiceGachaWishGroupId', 'rateChoiceGachaWishes', None),
    ):
        group_id = raw.get(field)
        if not group_id:
            continue
        group = [row for row in tables.get(name, []) if row['groupId'] == group_id]
        if not group:
            raise GachaExtensionDataError(name, group_id)
        if name == 'gachaFreebieGroups':
            _get_gacha_freebie_group(raw, group)
        for row in group:
            if purpose and (name == 'gachaFreebieGroups' or row.get('gachaBonusRewardType') == 'item'):
                if not row.get('resourceBoxId'):
                    raise GachaExtensionDataError(name, f'{group_id}/resourceBoxId')
                selected_boxes.add((purpose, row['resourceBoxId']))
    if raw['gachaType'] == 'gift':
        gifts = [row for row in tables.get('giftGachaExchanges', []) if row['gachaId'] == raw['id']]
        if not gifts:
            raise GachaExtensionDataError('giftGachaExchanges', raw['id'])
        for row in gifts:
            if not row.get('resourceBoxId'):
                raise GachaExtensionDataError('giftGachaExchanges', f'{raw["id"]}/resourceBoxId')
            selected_boxes.add(('gift_gacha_extra', row['resourceBoxId']))
    # 只保存本池引用的奖励盒；空奖励盒同样不能用来展示完整机制。
    boxes = {(row['resourceBoxPurpose'], row['id']): row for row in tables.get('resourceBoxes', [])
             if (row['resourceBoxPurpose'], row['id']) in selected_boxes}
    for key in selected_boxes:
        if key not in boxes or not boxes[key].get('details'):
            raise GachaExtensionDataError('resourceBoxes', f'{key[0]}/{key[1]}')
    return selected_boxes


def get_ticket_exchange_cards(region, ticket):
    """从票券实际消耗关联兑换奖励，返回完整四星集合及完整性状态。"""
    boxes = {(row['resourceBoxPurpose'], row['id']): row for row in raw_table(region, 'resourceBoxes')}
    cards = {row['id']: row for row in raw_table(region, 'cards')}
    identifiers = set()
    matched = False
    complete = True
    for summary in raw_table(region, 'materialExchangeSummaries'):
        for exchange in summary.get('materialExchanges', []):
            if not any(cost.get('resourceType') == ticket['resourceType'] and
                       cost.get('resourceId') == ticket.get('resourceId') for cost in exchange.get('costs', [])):
                continue
            matched = True
            reward_box = boxes.get(('material_exchange', exchange['resourceBoxId']))
            if reward_box is None:
                complete = False
                continue
            for reward in reward_box.get('details', []):
                if reward['resourceType'] != 'card':
                    continue
                card = cards.get(reward['resourceId'])
                if card is None:
                    complete = False
                elif card['cardRarityType'] == 'rarity_4':
                    identifiers.add(card['id'])
    return identifiers, complete and matched and bool(identifiers)


def get_gacha_exchange_entries(raw, region):
    """汇总整套阶梯的成员自选券，保留实际获得阶段与兑换范围。"""
    stages = gacha_stage_rows(raw, {'gachas': raw_table(region, 'gachas'),
                                  'gachaTabs': raw_table(region, 'gachaTabs')})
    materials = {row['id']: row for row in raw_table(region, 'materials')}
    entries = {}
    for index, stage in enumerate(stages, 1):
        for reward in extra_rewards(stage, region):
            if reward['resourceType'] not in ('material', 'gacha_ticket'):
                continue
            key = reward['resourceType'], reward.get('resourceId')
            material = materials.get(reward.get('resourceId'), {}) if reward['resourceType'] == 'material' else {}
            card_ids, complete = get_ticket_exchange_cards(region, reward)
            known_ticket = material.get('materialType') == 'card_ticket' or any(
                word in material.get('name', '') for word in ('自选券', '成员兑换券', 'メンバーチケット', 'メンバーセレクトチケット'))
            if not card_ids and not known_ticket:
                continue
            if key not in entries:
                entries[key] = dict(ticket=reward, card_ids=card_ids, complete=complete, origins=[])
            entries[key]['origins'].append(dict(stage=stage, number=index, quantity=reward['resourceQuantity']))
    return list(entries.values())


async def draw_gacha_exchange_entry(ctx, raw, entry, width):
    """展示实际券和兑换范围，阶梯面板已列出的获得阶段不再重复。"""
    if raw['gachaType'] not in ('sureturn', 'subeginner', 'sunormal'):
        counts = {behavior['spinCount'] for behavior in raw['gachaBehaviors'] if behavior.get('gachaExtraId')}
        line('完成十连招募后获得' if counts == {10} else '完成指定招募后获得', width)
    await reward_row(ctx, entry['ticket'], width)
    ticket = entry['ticket']
    boxes = {(row['resourceBoxPurpose'], row['id']): row for row in raw_table(ctx.region, 'resourceBoxes')}
    rules = set()
    for summary in raw_table(ctx.region, 'materialExchangeSummaries'):
        for exchange in summary.get('materialExchanges', []):
            costs = exchange.get('costs', [])
            if len(costs) != 1 or costs[0].get('resourceType') != ticket['resourceType'] or costs[0].get('resourceId') != ticket.get('resourceId'):
                continue
            rewards = boxes.get(('material_exchange', exchange['resourceBoxId']), {}).get('details', [])
            if len(rewards) == 1 and rewards[0]['resourceType'] == 'card':
                rules.add((costs[0].get('quantity'), rewards[0]['resourceQuantity']))
    if len(rules) == 1 and rules != {(1, 1)}:
        cost, quantity = next(iter(rules))
        line(f'每{cost}张券可兑换{quantity}名★4成员', width)
    await ticket_selection_scope(ctx, raw, entry['ticket'], width)


async def _load_gacha_render_data(ctx, gacha):
    """仅特殊机制按需获取关联表，网络等待继续走现有异步MasterData管理器。"""
    tables = {'gachas': [gacha.raw], '_unavailable_tables': []}
    required = set()
    raw = gacha.raw

    async def load_tables(names):
        """并发读取同阶段独立表，完成后按名称合并本请求的数据和失败状态。"""
        async def load_table(name):
            try:
                rows = ([row.raw for row in await ctx.md.gachas.get()] if name == 'gachas'
                        else await ctx.md.get(name))
                if name == 'resourceBoxes' and isinstance(rows, dict):
                    rows = [row for boxes in rows.values() for row in boxes.values()]
                return name, rows, False
            except Exception as error:
                logger.print_exc(f'卡池关联数据读取失败：{ctx.region}/{name}：{error}')
                return name, [], True

        for name, rows, failed in await _gacha_gather(*(load_table(name) for name in sorted(names))):
            tables[name] = rows
            if failed:
                tables['_unavailable_tables'].append(name)
    if any(row.get('gachaExtraId') for row in raw.get('gachaBehaviors', [])):
        required.update(('gachaExtras', 'resourceBoxes'))
    if raw.get('wishSelectCount') or raw.get('rateChoiceGachaWishGroupId'):
        required.add('rateChoiceGachaWishes')
    if raw.get('gachaCharacterBonusGroupId'):
        required.add('materials')
    if raw.get('gachaFreebieGroupId'):
        required.update(('gachaFreebieGroups', 'resourceBoxes', 'materials'))
    if raw.get('gachaBonusItemReceivableRewardGroupId'):
        required.update(('gachaBonusPoints', 'gachaBonusItemReceivableRewards', 'cardSupplyGroups',
                         'resourceBoxes', 'materials'))
    if raw.get('gachaType') == 'gift':
        required.update(('giftGachaExchanges', 'resourceBoxes', 'materials'))
    if raw.get('gachaType') in ('sureturn', 'subeginner', 'sunormal'):
        required.update(('gachaTabs', 'gachaExtras', 'resourceBoxes', 'gachas'))
    await load_tables(required)
    if tables['_unavailable_tables']:
        return tables
    try:
        selected_boxes = _validate_gacha_render_data(raw, tables)
    except GachaExtensionDataError as error:
        _mark_gacha_extension_unavailable(ctx, gacha, tables, error)
        return tables
    # 引用完整后再加载奖励名称，缺记录时直接保留基础详情。
    kinds = {detail['resourceType'] for row in tables.get('resourceBoxes', [])
             if (row['resourceBoxPurpose'], row['id']) in selected_boxes for detail in row.get('details', [])}
    names = {'gacha_ticket': 'gachaTickets', 'practice_ticket': 'practiceTickets',
             'skill_practice_ticket': 'skillPracticeTickets', 'boost_item': 'boostItems',
             'material': 'materials', 'costume_3d': 'costume3ds'}
    name_tables = {names[kind] for kind in kinds if kind in names}
    await load_tables(name_tables - set(tables))
    selected_costumes = {detail['resourceId'] for row in tables.get('resourceBoxes', [])
                         if (row['resourceBoxPurpose'], row['id']) in selected_boxes
                         for detail in row.get('details', []) if detail['resourceType'] == 'costume_3d'}
    if selected_costumes and any(not row.get('name') for row in tables.get('costume3ds', [])
                                 if row['id'] in selected_costumes):
        # Compact主表把名称拆到分组表；已有名称的完整主表无需读取不存在的分组端点。
        await load_tables({'costume3dGroups'})
    selected_materials = {detail['resourceId'] for row in tables.get('resourceBoxes', [])
        if row['resourceBoxPurpose'] == 'gacha_extra' and (row['resourceBoxPurpose'], row['id']) in selected_boxes
        for detail in row.get('details', []) if detail['resourceType'] == 'material'}
    tickets = [row for row in tables.get('materials', []) if row['id'] in selected_materials
               and (row.get('materialType') == 'card_ticket' or
                    any(word in row.get('name', '') for word in ('メンバーチケット', 'セレクトチケット', '自选', '成员兑换券')))]
    if tickets or 'gacha_ticket' in kinds:
        await load_tables({'materialExchangeSummaries', 'cards', 'eventCards', 'events'} - set(tables))
    return tables


async def compose_gacha_detail_image(ctx: SekaiHandlerContext, gacha: Gacha, *, full=False, conditions='', page=1,
                                   exchange=False, show_unowned=False, personal_profile=None, trace_id=None):
    """准备特殊机制和可选完整名单，请求数据通过ContextVar隔离。"""
    full = full or exchange or show_unowned
    trace_id = trace_id or f'{ctx.region}/{gacha.id}/{time.monotonic_ns()}'
    limit_token = ASSET_REQUEST_LIMIT.set(_GACHA_PREPARE_LIMIT)
    timings = []
    timing_token = ASSET_PREPARE_TIMINGS.set(timings)
    token = None
    started = time.perf_counter()
    try:
        tables, base_images = await _gacha_gather(_load_gacha_render_data(ctx, gacha),
                                                 _prepare_gacha_base_images(ctx, gacha))
        tables['_base_images'] = base_images
        token = _GACHA_RENDER_DATA.set((ctx, tables))
        prepared = time.perf_counter()
        full_data = await prepare_gacha_full_list(ctx, gacha, conditions, exchange=exchange) if full else None
        if full_data is not None and show_unowned and personal_profile is None:
            personal_profile, _ = await get_detailed_profile(
                ctx, ctx.user_id, filter=get_detailed_profile_card_filter('userCards'),
                mode=getattr(ctx, 'data_mode_arg', None), raise_exc=True,
            )
            assert_and_reply(personal_profile is not None, '未获取到抓包数据，无法筛选未持有成员')
        if full_data is not None and personal_profile is not None:
            assert_and_reply('userCards' in personal_profile, '抓包数据缺少持有卡牌，无法筛选未持有成员')
            owned_ids = {card['cardId'] for card in personal_profile['userCards']}
            full_data['ordered_ids'] = [card_id for card_id in full_data['ordered_ids'] if card_id not in owned_ids]
            full_data['matched'] = len(full_data['ordered_ids'])
            full_data['personal_profile'] = personal_profile
        listed = time.perf_counter()
        image = await _compose_gacha_detail_image(ctx, gacha, full_data, page)
        logger.info(f'卡池绘图耗时 {trace_id}: 数据及基础图片={prepared-started:.3f}s '
                    f'名单及持卡={listed-prepared:.3f}s 绘制及其余素材={time.perf_counter()-listed:.3f}s')
        if timings:
            logger.info(f'卡池资源耗时 {trace_id}: {timings}')
        return image
    finally:
        if token is not None:
            _GACHA_RENDER_DATA.reset(token)
        ASSET_REQUEST_LIMIT.reset(limit_token)
        ASSET_PREPARE_TIMINGS.reset(timing_token)


GACHA_TYPE_NAMES.update({'sureturn': '回归阶梯', 'subeginner': '新手阶梯', 'sunormal': '阶梯', 'return': '回归'})
ORIGINAL_WEIGHT = get_gacha_weight_info
NAMES = {
    ('material', 15): '心愿碎片', ('material', 16): '心愿纯结晶',
    ('material', 14): '奇迹宝石', ('material', 17): '魔法种子',
    ('material', 57): '心愿水滴', ('material', 168): '七彩记忆碎片',
    ('material', 282): '虚拟应援币', ('material', 320): '纪念★4成员兑换券',
    ('coin', None): '金币', ('boost_item', 2): '大火罐', ('music_card', 1): '音乐卡',
    ('practice_ticket', 2): '中级练习谱', ('skill_practice_ticket', 2): '中级技能谱',
    ('skill_practice_ticket', 3): '高级技能谱',
}

def box(region, purpose, resource_id):
    """按用途和编号读取奖励盒，缺失或为空时抛出 GachaExtensionDataError。"""
    result = next((row for row in raw_table(region, 'resourceBoxes')
                   if row['id'] == resource_id and row['resourceBoxPurpose'] == purpose), None)
    if result is None or not result.get('details'):
        raise GachaExtensionDataError('resourceBoxes', f'{purpose}/{resource_id}')
    return result

def extra_rewards(raw, region):
    """读取本池全部固定附赠，缺少附赠记录时抛出 GachaExtensionDataError。"""
    extras = {row['id']: row for row in raw_table(region, 'gachaExtras')}
    rewards = []
    for extra_id in dict.fromkeys(b['gachaExtraId'] for b in raw['gachaBehaviors'] if b.get('gachaExtraId')):
        extra = extras.get(extra_id)
        if extra is None or not extra.get('resourceBoxId'):
            raise GachaExtensionDataError('gachaExtras', extra_id)
        rewards.extend(box(region, 'gacha_extra', extra['resourceBoxId'])['details'])
    return rewards

async def resource_image(ctx, kind, resource_id):
    """使用现有道具图标读取器，票券补充实际已下载的缩略图路径。"""
    if kind in ('practice_ticket', 'skill_practice_ticket'):
        return await ctx.rip.img(f'thumbnail/{kind}/ticket{resource_id}.png')
    if kind == 'gacha_ticket':
        return await ctx.rip.img('thumbnail/gacha_ticket/gacha_ticket.png')
    return await get_res_icon(ctx, kind, resource_id)

def resource_name(region, kind, resource_id):
    if name := NAMES.get((kind, resource_id)):
        return name
    tables = {'material': 'materials', 'boost_item': 'boostItems', 'gacha_ticket': 'gachaTickets',
              'practice_ticket': 'practiceTickets', 'skill_practice_ticket': 'skillPracticeTickets'}
    if kind in tables:
        row = next((row for row in raw_table(region, tables[kind]) if row['id'] == resource_id), None)
        if row:
            name = row.get('name', kind)
            if 'ミュージックカード' in name:
                return '音乐卡'
            if 'メモリアルフラグメント' in name:
                return '纪念碎片'
            if 'ノーブルフェイヴバルーン' in name:
                return '对应角色的高贵气球'
            if 'ロイヤルフェイヴバルーン' in name:
                return '对应角色的皇家气球'
            return name
    if kind == 'coin':
        return '金币'
    if kind == 'music_card':
        return '音乐卡'
    raise ValueError(f'未识别的道具名称：{kind}/{resource_id}')

def text_style(size=20, bold=False):
    return TextStyle(font=DEFAULT_BOLD_FONT if bold else DEFAULT_FONT, size=size, color=(50, 50, 50) if bold else (70, 70, 70))

def line(text, width, bold=False):
    return TextBox(text, text_style(bold=bold), use_real_line_count=True).set_w(width)

@contextmanager
def panel(title, width):
    # 原版单栏会通过item_bg给子项画底板；此时不得再叠一层自身背景。
    parent = VSplit.get_current_widget()
    background = None if getattr(parent, 'item_bg', None) is not None else roundrect_bg()
    with VSplit().set_w(width + 32).set_padding(16).set_sep(10).set_content_align('l').set_item_align('l').set_bg(background) as widget:
        TextBox(title, text_style(size=22, bold=True))
        yield widget

async def reward_row(ctx, reward, width, probability=None, prefix=''):
    kind, resource_id = reward['resourceType'], reward.get('resourceId')
    name = resource_name(ctx.region, kind, resource_id)
    with HSplit().set_w(width).set_sep(10).set_content_align('l').set_item_align('c'):
        ImageBox(await resource_image(ctx, kind, resource_id), size=(36, 36))
        line(f"{prefix}{name} ×{reward['resourceQuantity']}", width - (150 if probability is not None else 46))
        if probability is not None:
            # 15.89%在20px字体下超过62px；固定足宽列，不能用省略号隐藏概率尾数。
            TextBox(f'{probability:g}%', text_style()).set_w(94).set_content_align('r')

async def activity_label(when, ctx):
    """把官方截止日期对应到当时的箱活简称，找不到对应活动则只显示日期。"""
    stamp = int(when.timestamp() * 1000)
    events = await ctx.md.events.get()
    # JP招募常在活动同日中午开放，活动本身通常在15点开始。
    event = next((row for row in events if datetime.fromtimestamp(row['startAt'] / 1000, JST).date() == when.date()), None)
    if not event:
        event = next((row for row in events if row['startAt'] <= stamp <= row['aggregateAt']), None)
    if not event:
        event = max((row for row in events if row['aggregateAt'] <= stamp), key=lambda row: row['aggregateAt'], default=None)
    if not event:
        return ''
    cid = await get_event_banner_chara_id(ctx, event)
    if not cid:
        return ''
    bans = await get_chara_ban_events(ctx, cid)
    match = next((row for row in bans if row['id'] == event['id']), None)
    return f"{get_character_first_nickname(cid)}{match['ban_index']}箱" if match else ''

async def cutoff_lines(raw, region='jp'):
    """只读取官方说明明确给出的登场截止日期，不从最后一张卡倒推日期。"""
    result = []
    text = raw['gachaInformation'].get('summary', '')
    # 兑换碎片的范围属于另一机制，不能混入UP名单的可选范围。
    for heading in ('■ガチャシールについて', '■メモリアルフラグメントについて', '■ガチャボーナス'):
        text = text.split(heading, 1)[0]
    pattern = re.compile(r'(20\d{2})[年/](\d{1,2})[月/](\d{1,2})(?:日)?(?:\s*(\d{1,2})(?:時|:)(\d{2}))?')
    for source in text.splitlines():
        if not any(word in source for word in ('まで', '截止至')) or not any(word in source for word in ('登場', 'ガチャに', '登场')):
            continue
        if not (match := pattern.search(source)):
            continue
        year, month, day, hour, minute = match.groups()
        when = datetime(int(year), int(month), int(day), int(hour or 12), int(minute or 0), tzinfo=JST)
        # CN说明中的日期不再套日服活动日历，避免标成不对应的箱活。
        label = await activity_label(when, _GACHA_RENDER_DATA.get()[0]) if region == 'jp' else ''
        date = when.strftime('%Y-%m-%d') + (f'（{label}）' if label else '')
        if '2回以上復刻' in source:
            value = f"{when.strftime('%Y-%m-%d')}前已复刻至少2次的卡池中再登场的期间限定★4"
            if value not in result:
                result.append(value)
            continue
        if ('ブルームフェスティバル' in source or 'BFes' in source) and '恒常' not in source:
            scope = 'BFes、CFes限定'
        elif '恒常' in source and 'ユニットイベント限定' in source:
            scope = '常驻及组合活动限定★4'
        elif '恒常' in source or '常规成员' in source:
            scope = '常驻★4'
        elif '期間限定' in source:
            scope = '期间限定★4'
        else:
            scope = '指定范围的★4'
        value = f'{date}前登场的{scope}'
        if value not in result:
            result.append(value)
    return result

def single_column(raw, region):
    """仅自选规则、奖励条或开放限制不再单开右栏；复杂组合机制仍保留双栏。"""
    if raw_table(region, '_unavailable_tables'):
        return True
    tags = set(classify(raw, region))
    if not tags:
        return True
    if tags in ({'旧式奖励条'}, {'开放时限'}):
        return True
    rules = {'自选保底', '自选UP', '主副选UP', '固定附赠'}
    return bool(tags & (rules - {'固定附赠'})) and tags <= rules and len(extra_rewards(raw, region)) <= 2

async def ticket_selection_scope(ctx, raw, ticket, width):
    """按票券实际消耗关联兑换所，而非把卡池的可抽名单误当成可兑换名单。"""
    ids, complete = get_ticket_exchange_cards(ctx.region, ticket)
    cards = {row['id']: row for row in raw_table(ctx.region, 'cards')}
    if not ids or not complete:
        line('兑换范围暂不可用', width)
        return
    pool_ids = {row['cardId'] for row in raw['gachaDetails'] if cards.get(row['cardId'], {}).get('cardRarityType') == 'rarity_4'}
    supply_ids = {cards[cid]['cardSupplyId'] for cid in ids}
    supply = '常驻★4' if supply_ids == {1} else '指定★4'
    description = f'可兑换{len(ids)}名{supply}'
    # 当实际集合恰好是卡池中的前段时，以末期箱活说明边界；不从CN异常releaseAt倒推。
    if ids == {cid for cid in pool_ids if cid <= max(ids)}:
        eid = next((row['eventId'] for row in raw_table(ctx.region, 'eventCards') if row['cardId'] == max(ids)), None)
        event = next((row for row in raw_table(ctx.region, 'events') if row['id'] == eid), None)
        if event:
            cid = await get_event_banner_chara_id(ctx, event)
            events = await get_chara_ban_events(ctx, cid) if cid else []
            match = next((row for row in events if row['id'] == eid), None)
            name = f"{get_character_first_nickname(cid)}{match['ban_index']}箱" if match else event['name']
            description += f'，范围至「{name}」'
    line(description, width)
    if ids <= pool_ids and pool_ids - ids:
        line(f'本卡池另有{len(pool_ids - ids)}名★4不在兑换范围', width)

def classify(raw, region):
    """按结构化字段组合规则，卡池编号只用于选择真实样本。"""
    tags = []
    lotteries = {row['lotteryType'] for row in raw['gachaCardRarityRates']}
    if raw.get('rateChoiceGachaWishGroupId'):
        tags.append('主副选UP')
    elif 'categorized_wish' in lotteries:
        tags.append('自选UP')
    elif raw.get('wishSelectCount'):
        tags.append('自选保底')
    if raw['gachaType'] in ('sureturn', 'subeginner', 'sunormal'):
        tags.append('阶梯')
    if raw.get('isSelectCharacter'):
        tags.append('指定角色')
    if raw['gachaType'] == 'gift':
        tags.append('自选附赠')
    if raw.get('gachaFreebieGroupId'):
        tags.append('随机附赠')
    if raw.get('gachaBonusItemReceivableRewardGroupId'):
        tags.append('累计奖励')
    elif raw.get('gachaBonusId'):
        tags.append('旧式奖励条')
    if raw.get('gachaCharacterBonusGroupId'):
        tags.append('角色奖励')
    if raw.get('drawableGachaHour'):
        tags.append('开放时限')
    if rewards := extra_rewards(raw, region):
        tags.append('附赠衣装' if any(row['resourceType'] == 'costume_3d' for row in rewards) else '固定附赠')
    return tags

async def draw_selection(ctx, raw, tags, width):
    with panel('自选UP名单' if '自选UP' in tags or '主副选UP' in tags else '自选规则', width):
        if '主副选UP' in tags:
            groups = [row for row in raw_table(ctx.region, 'rateChoiceGachaWishes') if row['groupId'] == raw['rateChoiceGachaWishGroupId']]
            for index, group in enumerate(sorted(groups, key=lambda row: row['seq'])):
                total = sum(row['rate'] for row in raw['gachaCardRarityRates'] if row['lotteryType'] == group['lotteryType'])
                line(f"{'主选' if index == 0 else '副选'}{group['selectCount']}位，每位{total / group['selectCount']:g}%", width, True)
            line('设置完全部位置后才能招募', width)
        elif '自选UP' in tags:
            total, fixed = raw['wishSelectCount'], raw.get('wishFixedSelectCount', 0)
            line(f'固定{fixed}位，自选{total - fixed}位', width, True)
            limit = raw.get('wishLimitedSelectCount', 0)
            if limit:
                line(f'自选位置中最多可选{limit}名指定Fes限定', width)
            if '変更できません' in raw['gachaInformation'].get('summary', '') or '変更することはできません' in raw['gachaInformation'].get('summary', ''):
                line('名单确认后不可更改', width)
        else:
            count = raw['wishSelectCount']
            source = raw['gachaInformation'].get('summary', '')
            scope = '期间限定' if '期間限定★4' in source and '恒常★4' not in source else '★4'
            line(f'选择{count}名{scope}成员', width, True)
            line('十连至少获得其中1名', width)
        if ranges := await cutoff_lines(raw, ctx.region):
            line('可选范围', width, True)
            for value in ranges[:3]:
                line(value, width)

async def draw_steps(ctx, raw, width):
    stages = gacha_stage_rows(raw, {'gachas': raw_table(ctx.region, 'gachas'),
                                   'gachaTabs': raw_table(ctx.region, 'gachaTabs')})
    with panel('回归阶梯招募' if raw['gachaType'] == 'sureturn' else '新手阶梯招募', width):
        line(f'依次完成{len(stages)}个阶段，每阶段限1次', width)
        for index, stage in enumerate(stages, 1):
            behavior = stage['gachaBehaviors'][0]
            # STEP仅负责分段，使用外层阶梯面板的底板，避免额外套第三层玻璃。
            with VSplit().set_w(width).set_padding(10).set_sep(5).set_content_align('l').set_item_align('l'):
                line(f"STEP {index}　{behavior['spinCount']}抽", width - 20, True)
                with HSplit().set_sep(8).set_content_align('l').set_item_align('c'):
                    ImageBox(await resource_image(ctx, behavior['costResourceType'], behavior.get('costResourceId')), size=(30, 30))
                    TextBox(f"{'付费' if 'paid' in behavior['costResourceType'] else ''} ×{behavior['costResourceQuantity']}", text_style())
                if stage.get('wishSelectCount'):
                    line(f"自选{stage['wishSelectCount']}名★4中至少获得1名", width - 20)
                else:
                    guaranteed = '★4' if behavior['gachaBehaviorType'] == 'over_rarity_4_once' else '★3'
                    line(f'至少获得1名{guaranteed}', width - 20)
                for reward in extra_rewards(stage, ctx.region):
                    await reward_row(ctx, reward, width - 20, prefix='附赠 ')
        for value in await cutoff_lines(raw, ctx.region):
            line(('自选范围：' if raw.get('wishSelectCount') else '卡池范围：') + value, width)

async def draw_character_rewards(ctx, raw, width):
    """公开主表没有角色奖励关联表，必须从本池官方说明解析数量，不能按组号猜。"""
    source = raw['gachaInformation'].get('summary', '')
    materials = raw_table(ctx.region, 'materials')
    pattern = re.compile(r'★([234])メンバー1人(?:獲得|出現)につき、(?:そのキャラクターの)?「([^」]+)」を([\d,]+)個')
    entries = []
    for rarity, name, quantity in pattern.findall(source):
        material = next((row for row in materials if row.get('name') == name), None)
        if material is None:
            material = next((row for row in materials if row.get('name', '').startswith(name)), None)
        if material is not None:
            entries.append((rarity, material['id'], int(quantity.replace(',', ''))))
    with panel('纪念碎片' if 'メモリアルフラグメント' in source else '角色附赠奖励', width):
        if 'フェイヴバルーン' in source:
            line('按本次招募获得的角色发放', width)
        for rarity, material_id, quantity in entries:
            with HSplit().set_sep(8).set_content_align('l').set_item_align('c'):
                TextBox(f'★{rarity}', text_style(bold=True)).set_w(40)
                await reward_row(ctx, {'resourceType': 'material', 'resourceId': material_id,
                                      'resourceQuantity': quantity}, width - 48)
        if not entries:
            # 未识别新说明时保留原文的奖励段，不编造当前样本的数量。
            section = next((part for part in source.split('■') if 'について' in part and any(
                word in part for word in ('バルーン', 'フラグメント'))), '')
            for value in section.splitlines()[1:]:
                if value.strip():
                    line(value, width)
        if raw.get('isSelectCharacter') and '想いのカケラ' in source:
            line('重复成员另附赠对应角色的心愿碎片', width)

async def draw_fixed_rewards(ctx, raw, rewards, width):
    """显示实际附赠，自选券的获得方式、查询指引和兑换范围集中展示。"""
    entries = get_gacha_exchange_entries(raw, ctx.region)
    ticket_keys = {(entry['ticket']['resourceType'], entry['ticket'].get('resourceId')) for entry in entries}
    with panel('自选兑换' if entries else '附赠奖励', width):
        for entry in entries:
            await draw_gacha_exchange_entry(ctx, raw, entry, width)
        materials = {row['id']: row for row in raw_table(ctx.region, 'materials')}
        bottles = [row for row in rewards if row['resourceType'] == 'material'
                   and any(word in materials.get(row['resourceId'], {}).get('name', '')
                           for word in ('想いの小瓶', '心愿小瓶'))]
        if len({row['resourceQuantity'] for row in bottles}) > 1:
            bottles = []
        for reward in rewards:
            if reward not in bottles and (reward['resourceType'], reward.get('resourceId')) not in ticket_keys:
                await reward_row(ctx, reward, width)
        if bottles:
            line(f"全{len(bottles)}名角色的心愿小瓶，各{bottles[0]['resourceQuantity']}个", width)

async def draw_gift_choice(ctx, raw, width):
    choices = sorted((row for row in raw_table(ctx.region, 'giftGachaExchanges') if row['gachaId'] == raw['id']), key=lambda row: row['seq'])
    with panel('十连附赠', width):
        line(f'每次十连前，从以下{len(choices)}项中选择1项', width)
        line('完成十连后，获得所选道具及对应数量', width)
        with Grid(col_count=2).set_sep(12, 12).set_content_align('l').set_item_align('l'):
            for choice in choices:
                rewards = box(ctx.region, 'gift_gacha_extra', choice['resourceBoxId'])['details']
                with VSplit().set_w((width - 12) // 2).set_sep(5).set_content_align('l').set_item_align('l'):
                    for reward in rewards:
                        await reward_row(ctx, reward, (width - 12) // 2)

async def draw_random_gift(ctx, raw, width):
    group = _get_gacha_freebie_group(raw, raw_table(ctx.region, 'gachaFreebieGroups'))
    total = sum(row['weight'] for row in group)
    with panel('随机附赠奖励', width):
        line('每抽1次，独立随机获得以下1项奖励', width)
        if raw.get('spinLimit'):
            line(f"最多招募{raw['spinLimit']}次", width)
        for row in group:
            rewards = box(ctx.region, 'gacha_freebie_group', row['resourceBoxId'])['details']
            for reward in rewards:
                await reward_row(ctx, reward, width, row['weight'] * 100 / total)

async def draw_bonus(ctx, raw, tags, width):
    """累积点数和奖励读取关联表，旧奖励条读取本池明确的官方说明。"""
    if '累计奖励' in tags:
        with panel('卡池范围', width):
            for value in await cutoff_lines(raw, ctx.region):
                line(value, width)
    with panel('累计招募奖励' if '累计奖励' in tags else '招募奖励', width):
        point_names = {'jewel': '免费水晶', 'paid_jewel': '付费水晶', 'gacha_ticket': '招募券'}
        point_groups = collections.defaultdict(list)
        points = list(raw_table(ctx.region, 'gachaBonusPoints'))
        if not points:
            # 旧版国服未分发这张表，只采用本池说明中明确写出的点数，不借用JP规则。
            source = raw['gachaInformation'].get('summary', '')
            labels = {'無償クリスタル': 'jewel', '有償クリスタル': 'paid_jewel',
                      '免费水晶': 'jewel', '付费水晶': 'paid_jewel', '招募券': 'gacha_ticket'}
            for label, value in re.findall(r'(無償クリスタル|有償クリスタル|免费水晶|付费水晶|招募券)[：:]\s*[＋+]?([\d.]+)', source):
                points.append({'resourceType': labels[label], 'point': float(value)})
            match = re.search(r'ガチャチケット[^\n]*[＋+]([\d.]+)', source)
            if match:
                points.append({'resourceType': 'gacha_ticket', 'point': float(match[1])})
        for point in points:
            if point['resourceType'] in point_names:
                point_groups[point['point']].append(point_names[point['resourceType']])
        for point, names in point_groups.items():
            line(f"{'／'.join(names)}：每抽{point:g}点", width)
        if '累计奖励' in tags:
            entries = [row for row in raw_table(ctx.region, 'gachaBonusItemReceivableRewards') if row['groupId'] == raw['gachaBonusItemReceivableRewardGroupId']]
            labels = {'not_owned_random_rarity4_fixed': '随机1名未持有★4', 'selectable_rarity4_fixed': '自选1名★4'}
            items = collections.defaultdict(list)
            for entry in entries:
                if entry['gachaBonusRewardType'] == 'item':
                    rewards = box(ctx.region, 'gacha_bonus_item_receivable_reward', entry['resourceBoxId'])['details']
                    identity = tuple((item['resourceType'], item.get('resourceId'), item['resourceQuantity']) for item in rewards)
                    items[identity].append(entry['gachaBonusBorderPoint'])
                    continue
                text = labels[entry['gachaBonusRewardType']]
                supplies = {row['cardSupplyId'] for row in raw_table(ctx.region, 'cardSupplyGroups')
                            if row['groupId'] == entry.get('cardSupplyGroupId')}
                if supplies == {1}:
                    text += '，常驻范围'
                else:
                    text += '，包含指定限定'
                with HSplit().set_sep(12).set_content_align('l').set_item_align('c'):
                    TextBox(f"{entry['gachaBonusBorderPoint']}点", text_style(bold=True)).set_w(66)
                    line(text, width - 78)
            for identity, points in items.items():
                import math
                step = math.gcd(*points)
                other_points = {row['gachaBonusBorderPoint'] for row in entries if row['gachaBonusRewardType'] != 'item'}
                expected = set(range(step, max(row['gachaBonusBorderPoint'] for row in entries) + 1, step)) - other_points
                if len(points) > 1 and set(points) == expected:
                    line(f'其余每{step}点：', width)
                else:
                    line('、'.join(str(point) for point in points) + '点：', width)
                for kind, resource_id, quantity in identity:
                    await reward_row(ctx, {'resourceType': kind, 'resourceId': resource_id,
                                           'resourceQuantity': quantity}, width)
            line('没有未持有成员时，从对应完整范围抽取', width)
        else:
            source = raw['gachaInformation'].get('summary', '')
            for points, content in re.findall(r'ガチャボーナスが(\d+)たまると、([^\n]+)', source):
                text = '当期★4成员×1' if '新登場' in content else '★4成员×1'
                with HSplit().set_sep(12).set_content_align('l').set_item_align('c'):
                    TextBox(f'{points}点', text_style(bold=True)).set_w(66)
                    line(text, width - 78)

def costume_image_role_key(ctx, raw, relative, index):
    """取得单张衣装素材的路径、尺寸和修改时间，供用途记录校验。"""
    locate = getattr(ctx.rip, 'get_local_asset_cache_path', None)
    if locate is None:
        return None
    path = locate(f"{relative}/bg_gacha{raw['id']}_{index}.png")
    if not path:
        return None
    try:
        stat = Path(path).stat()
    except OSError:
        return None
    return (ctx.region, raw['assetbundleName'], str(Path(path).resolve()), stat.st_size, stat.st_mtime_ns)


def costume_role_cache_key(ctx, raw, relative):
    """双图版本齐全时复用分类缓存，单图缺失由独立用途记录处理。"""
    first = costume_image_role_key(ctx, raw, relative, 1)
    second = costume_image_role_key(ctx, raw, relative, 2)
    return (first, second) if first is not None and second is not None else None

async def draw_costume(ctx, raw, width):
    """类型确认与裁切回退分开：未知图1占位，不能盲裁或直接放整张原图。"""
    # CDN 仍使用 texture 路径；资源管理器将本地读取与裁框指纹统一到当前导出文件。
    relative = f"gacha/{raw['assetbundleName']}/screen/texture"
    stem = raw['assetbundleName'].removeprefix('ab_')
    single_path = f'{relative}/bg_{stem}.png'
    single_first = ctx.region == 'cn' and raw['assetbundleName'].startswith('ab_gacha_cn_')
    if single_first:
        single = await get_gacha_asset_image(ctx, single_path, default=None)
        if single is not None:
            await draw_single_costume_lineup(ctx, raw, single, single_path, width)
            return
    first, second = await _gacha_gather(
        get_gacha_asset_image(ctx, f"{relative}/bg_gacha{raw['id']}_1.png", default=None),
        get_gacha_asset_image(ctx, f"{relative}/bg_gacha{raw['id']}_2.png", default=None))
    # 国服专有衣装包使用一张多人展示背景，img_是宣传文字层，不能当作第二张试穿图。
    if first is None and second is None and ctx.region == 'cn' and not single_first:
        single = await get_gacha_asset_image(ctx, single_path, default=None)
        if single is not None:
            await draw_single_costume_lineup(ctx, raw, single, single_path, width)
            return
    title_first, title_second = await _gacha_gather(
        get_gacha_asset_image(ctx, f"{relative}/cardname_gacha{raw['id']}_1.png", default=None),
        get_gacha_asset_image(ctx, f"{relative}/cardname_gacha{raw['id']}_2.png", default=None))
    # 读取用途缓存和原子保存均在线程中执行，避免同步磁盘等待占用命令事件循环。
    classification = await asyncio.to_thread(
        resolve_costume_pair, first, second, title_first is not None and title_second is not None,
        costume_role_cache_key(ctx, raw, relative),
        first_image_key=costume_image_role_key(ctx, raw, relative, 1),
        role_cache_path=Path('data/sekai/gacha/costume_roles.json'),
    )
    with panel('附赠衣装', width):
        for index, image in enumerate((first, second), 1):
            unknown_first = index == 1 and classification.role == 'unknown'
            cropped = index == 2 or classification.role == 'dual_lineup'
            bounds = None
            route = 'illustration'
            if image is not None and (not unknown_first) and cropped:
                try:
                    from .costume_runtime import get_runtime
                    runtime = get_runtime(Path('data/sekai/gacha'))
                    source_path = getattr(ctx.rip, 'get_local_asset_cache_path', lambda path: None)(f"{relative}/bg_gacha{raw['id']}_{index}.png")
                    if source_path:
                        stat = Path(source_path).stat()
                        version = (str(source_path), stat.st_size, stat.st_mtime_ns)
                    else:
                        import hashlib
                        version = await asyncio.to_thread(lambda: hashlib.sha256(image.tobytes()).hexdigest())
                    result = await runtime.bounds(image, version)
                    bounds, route = (result.bounds, result.route)
                    left, top, right, bottom = bounds
                    if not (0 <= left < right <= image.width and 0 <= top < bottom <= image.height):
                        raise ValueError('衣装裁框超出原图')
                except (KeyError, ValueError, TypeError):
                    bounds = fixed_bounds(image.size)
                    route = 'fixed_fallback'
            if image is None or unknown_first:
                artwork_slot = index == 1 and classification.role == 'illustration_and_lineup'
                height = round(width * (1440 / 2520 if artwork_slot else 671 / 868))
                ImageBox(UNKNOWN_IMG, size=(width, height), image_size_mode='fit').set_content_align('c')
                route = 'missing_placeholder' if image is None else 'unclassified_placeholder'
                display_size = (width, height)
            else:
                rendered = image.crop(bounds) if bounds else image
                ImageBox(rendered, size=(width, None), image_size_mode='fit')
                display_size = rendered.size
        await draw_gacha_costume_names(ctx, raw, width)


async def draw_single_costume_lineup(ctx, raw, image, relative, width):
    """国服单图使用横向多人裁框，一张图展示全部男女款衣装。"""
    from .costume_runtime import get_runtime
    runtime = get_runtime(Path('data/sekai/gacha'))
    path = getattr(ctx.rip, 'get_local_asset_cache_path', lambda relative: None)(relative)
    if path:
        stat = Path(path).stat()
        version = (str(path), stat.st_size, stat.st_mtime_ns)
    else:
        import hashlib
        version = await asyncio.to_thread(lambda: hashlib.sha256(image.tobytes()).hexdigest())
    result = await runtime.bounds(image, version, layout='wide')
    with panel('附赠衣装', width):
        ImageBox(image.crop(result.bounds), size=(width, None), image_size_mode='fit')
        await draw_gacha_costume_names(ctx, raw, width)


async def draw_gacha_costume_names(ctx, raw, width):
    """按实际衣装奖励关联官方名称与身体部位缩略图，同名颜色变体合并展示。"""
    costume_ids = {reward['resourceId'] for reward in extra_rewards(raw, ctx.region)
                   if reward['resourceType'] == 'costume_3d'}
    groups = {row['groupId']: row for row in raw_table(ctx.region, 'costume3dGroups')}
    rows = await ctx.md.costume3ds.collect_by_ids(costume_ids)
    by_name = {}
    for costume in sorted(rows, key=lambda row: row['id']):
        group = groups.get(costume.get('costume3dGroupId'), {})
        name = group.get('name') or costume.get('name')
        if name and costume.get('partType') == 'body' and costume.get('colorId', 1) == 1:
            by_name.setdefault(name, []).append(costume)
    if not by_name:
        source = raw.get('gachaInformation', {}).get('summary', '')
        sections = re.findall(r'(?:主题服装|限定衣装)\s*[“「『][^。\n]+', source)
        by_name = dict.fromkeys((name for section in sections for name in re.findall(r'[“「『]([^”」』]+)[”」』]', section)), [])
    for name, costumes in by_name.items():
        icon = None
        asset_names = list(dict.fromkeys(costume['assetbundleName'] for costume in costumes if costume.get('assetbundleName')))
        for asset_name in asset_names[:3]:
            icon = await get_gacha_asset_image(ctx, f'thumbnail/costume_rip/{asset_name}.png', default=None)
            if icon is not None:
                break
        with HSplit().set_w(width).set_sep(10).set_content_align('l').set_item_align('c'):
            ImageBox(icon if icon is not None else UNKNOWN_IMG, size=(36, 36), image_size_mode='fit')
            line(name, width - 46)

def balance_gacha_columns(left, sidebar):
    """将独立附赠奖励移至左栏，仅在整图变矮时调整布局。

    衣装、选择规则、阶梯及兑换引导留在原侧。奖励内容保持原宽度，
    外层面板填满左栏；移动后清除相关尺寸缓存。
    """
    panels = sidebar.items[0]
    left_width = left._get_self_size()[0] - 2 * (left.hpadding + left.hmargin)
    while len(panels.items) > 1:
        left_height = left._get_self_size()[1]
        right_height = sidebar._get_self_size()[1]
        current_height = max(left_height, right_height)
        candidates = []
        for index, item in enumerate(panels.items):
            if not item.userdata.get('gacha_movable_reward'):
                continue
            width, height = item._get_self_size()
            if width > left_width:
                continue
            projected_height = max(left_height + height + left.sep,
                                   right_height - height - panels.sep)
            if projected_height < current_height:
                candidates.append((projected_height, index, item))
        if not candidates:
            break
        _, _, item = min(candidates, key=lambda choice: choice[:2])
        panels.remove_item(item)
        item.set_w(left_width).set_bg(None)
        item._calc_w = item._calc_h = None
        insert_at = next((index for index, child in enumerate(left.items)
                          if child.userdata.get('gacha_query_guide')), len(left.items))
        left.add_item(item, insert_at)
        # 控件尺寸会向上缓存，修改列内容后必须一并重算外层布局。
        for column in (left, panels, sidebar):
            widget = column
            while widget is not None:
                widget._calc_w = widget._calc_h = None
                widget = widget.parent


async def _draw_extension(ctx, gacha, inline=False, inline_width=600):
    raw = raw_gacha(ctx.region, gacha.id)
    tags = classify(raw, ctx.region)
    rewards = extra_rewards(raw, ctx.region)
    content_w = 536 if any(tag in tags for tag in ('附赠衣装', '自选附赠', '随机附赠', '阶梯')) else 384
    if any(tag in tags for tag in ('累计奖励', '旧式奖励条')):
        content_w = 448
    if tags == ['开放时限']:
        content_w = 304
    if inline:
        # 原版消耗区可能撑宽整栏，新增面板也必须占满同一宽度，不能居中套窄框。
        content_w = inline_width - 32
    # 单栏直接追加同规格面板，不再套一个新的玻璃容器，也不搬动基础信息。
    container = nullcontext() if inline else VSplit().set_padding(0).set_sep(8).set_content_align("l").set_item_align("l")
    with container as sidebar:
        panels = nullcontext() if inline else VSplit().set_padding(8).set_sep(8).set_content_align("l").set_item_align("l").set_bg(roundrect_bg())
        with panels:
            if '阶梯' in tags:
                await draw_steps(ctx, raw, content_w)
                if entries := get_gacha_exchange_entries(raw, ctx.region):
                    with panel('自选兑换', content_w):
                        for entry in entries:
                            await draw_gacha_exchange_entry(ctx, raw, entry, content_w)
            elif any(tag in tags for tag in ('自选UP', '主副选UP', '自选保底')):
                await draw_selection(ctx, raw, tags, content_w)
            if '指定角色' in tags:
                with panel('角色选择', content_w):
                    line('选择1名角色，仅招募该角色的成员', content_w)
                    for value in await cutoff_lines(raw, ctx.region):
                        line(value, content_w)
            if '自选附赠' in tags:
                await draw_gift_choice(ctx, raw, content_w)
            if '随机附赠' in tags:
                await draw_random_gift(ctx, raw, content_w)
            if '固定附赠' in tags and '阶梯' not in tags:
                await draw_fixed_rewards(ctx, raw, rewards, content_w)
                if not inline and not get_gacha_exchange_entries(raw, ctx.region):
                    panels.items[-1].userdata['gacha_movable_reward'] = True
            if '附赠衣装' in tags:
                await draw_costume(ctx, raw, content_w)
            if '累计奖励' in tags or '旧式奖励条' in tags:
                await draw_bonus(ctx, raw, tags, content_w)
            if '角色奖励' in tags:
                await draw_character_rewards(ctx, raw, content_w)
                if not inline:
                    panels.items[-1].userdata['gacha_movable_reward'] = True
            if '开放时限' in tags and '阶梯' not in tags:
                with panel('开放条件', content_w):
                    hours = raw['drawableGachaHour']
                    if 'チュートリアル突破後' in raw['gachaInformation'].get('description', ''):
                        line(f'通过新手教程后{hours // 24}天内开放', content_w)
                    else:
                        line(f'可抽时限：{hours}小时', content_w)
                    limit = raw['gachaBehaviors'][0].get('executeLimit')
                    if limit:
                        line(f'每账号限{limit}次', content_w)
            entries = get_gacha_exchange_entries(raw, ctx.region)
            if entries:
                command = f'/{ctx.region}查卡池 {raw["id"]} 兑换'
                draw_gacha_query_guide([(f"发送“{command}”查看兑换名单", "")], inline_width if inline else content_w + 32)
    return sidebar

async def weight_info_for_display(ctx, gacha):
    """展示层合计自选UP的星级分支，保持原抽卡模拟函数不变。"""
    result = await ORIGINAL_WEIGHT(ctx, gacha)
    raw = raw_gacha(ctx.region, gacha.id)
    if any(row.lottery_type != 'normal' for row in gacha.rarity_rates):
        for rarity in result.rates:
            result.rates[rarity] = sum(row.rate for row in gacha.rarity_rates if row.rarity == rarity) / 100
        if result.guaranteed_type:
            result.guaranteed_rates = dict(result.rates)
            if result.guaranteed_type in ('rarity_3', 'rarity_4'):
                result.guaranteed_rates[result.guaranteed_type] += result.rates.get('rarity_2', 0)
                result.guaranteed_rates['rarity_2'] = 0
            if result.guaranteed_type == 'rarity_4':
                result.guaranteed_rates['rarity_4'] += result.rates.get('rarity_3', 0)
                result.guaranteed_rates['rarity_3'] = 0
        fixed_ids = {row['cardId'] for row in raw['gachaDetails'] if row.get('gachaDetailWishType') == 'fixed'}
        fixed_rate = sum(row['rate'] for row in raw['gachaCardRarityRates'] if row['lotteryType'] == 'categorized_wish') / 100 / max(1, raw.get('wishSelectCount', 0))
        for cards in result.cards.values():
            for card in cards:
                card.is_pickup = card.id in fixed_ids
                if card.is_pickup:
                    card.rate = card.guaranteed_rate = fixed_rate
    return result

async def draw_extension(ctx, gacha, inline=False, inline_width=600):
    """独立扩展失败不影响原卡池信息；异常仅记录后台。"""
    if raw_table(ctx.region, '_unavailable_tables'):
        return None
    parent = VSplit.get_current_widget()
    previous_items = list(parent.items)
    try:
        return await _draw_extension(ctx, gacha, inline, inline_width)
    except Exception as error:
        # 绘图组件进入上下文时已挂到父容器，失败时移除本次尚未完成的扩展。
        for item in list(parent.items):
            if item not in previous_items:
                parent.remove_item(item)
        if isinstance(error, GachaExtensionDataError):
            _mark_gacha_extension_unavailable(ctx, gacha, _GACHA_RENDER_DATA.get()[1], error)
        else:
            logger.print_exc(f'卡池扩展绘制失败：{ctx.region}/{gacha.id}：{error}')
        return None
