from datetime import datetime, timedelta, timezone

from ...utils import *
from ..common import *
from ..handler import *
from ..draw import *

RES_BOX_ICON_SCALE = {
    'jewel': 0.6, 'virtual_coin': 0.6, 'coin': 0.6,
    'boost_item': 0.6,
    'material': 0.6,
    'honor': 0.3,
    'stamp': 1.0,
}


async def get_res_icon(ctx: SekaiHandlerContext, res_type: str, res_id: int = None) -> Image.Image:
    """
    获取资源图标
    """
    res_image = UNKNOWN_IMG
    if res_type in ['jewel', 'paid_jewel', 'virtual_coin', 'coin']:
        if res_type == 'paid_jewel': res_type = 'jewel'
        res_image = await ctx.rip.img(f"thumbnail/common_material_rip/{res_type}.png", use_img_cache=True)
    elif res_type == 'boost_item':
        res_image = await ctx.rip.img(f"thumbnail/boost_item_rip/boost_item{res_id}.png", use_img_cache=True)               
    elif res_type == 'material':
        res_image = await ctx.rip.img(f"thumbnail/material_rip/material{res_id}.png", use_img_cache=True)
    elif res_type == 'gacha_ticket':
        asset_name = (await ctx.md.gacha_tickets.find_by_id(res_id))['assetbundleName']
        res_image = await ctx.rip.img(f"thumbnail/gacha_ticket/gacha_ticket.png", use_img_cache=True)
    elif res_type == 'gacha_item':
        asset_name = (await ctx.md.gacha_ceil_items.find_by_id(res_id))['assetbundleName']
        res_image = await ctx.rip.img(f"thumbnail/gacha_item/{asset_name}.png", use_img_cache=True)
    elif res_type == 'honor':
        asset_name = (await ctx.md.honors.find_by_id(res_id))['assetbundleName']
        res_image = await ctx.rip.img(f"honor/{asset_name}_rip/degree_main.png")
    elif res_type == 'stamp':
        asset_name = (await ctx.md.stamps.find_by_id(res_id))['assetbundleName']
        res_image = await ctx.rip.img(f"stamp/{asset_name}_rip/{asset_name}.png")
    return res_image


async def get_res_box_info(ctx: SekaiHandlerContext, purpose: str, bid: int, image_size: int = None) -> list:
    """
    获取资源箱信息，返回资源信息字典的列表，资源信息字段包括：
    - `type`: 资源类型
    - `id`: 资源ID
    - `quantity`: 资源数量
    - `image`: 资源图片
    """

    box = (await ctx.md.resource_boxes.get())[str(purpose)][int(bid)]
    box_type = box['resourceBoxType']
    ret = []

    if box_type == 'expand':
        for item in box['details']:
            res_type = item['resourceType']
            res_id = item.get('resourceId')
            res_quantity = item['resourceQuantity']
            
            try:
                res_image = await get_res_icon(ctx, res_type, res_id)
                if image_size:
                    res_image = resize_keep_ratio(res_image, image_size * RES_BOX_ICON_SCALE.get(res_type, 0.6), 'h')
            except Exception as e:
                res_image = UNKNOWN_IMG
                if image_size:
                    res_image = resize_keep_ratio(res_image, image_size * 0.6, 'h')
                logger.warning(f"获取资源 type={res_type} id={res_id} 图片失败: {e}")

            ret.append({
                'type': res_type,
                'id': res_id,
                'quantity': res_quantity,
                'image': res_image,
            })
    else:
        raise NotImplementedError()
    
    return ret


# ================================ 背包材料映射与分区 ================================ #

INVENTORY_SECTION_NAMES = {
    'common': '常用材料',
    'special_training': '特训材料',
    'master_lesson': '大师训练材料',
    'costume': '服装材料',
    'music': '音乐与翻唱',
    'vocal_card_ticket': '歌手卡兑换券',
    'character_rank_exp_ticket': '角色成长',
    'card_ticket': '成员兑换券',
    'gacha_ceil_ticket': '招募兑换券',
    'web_ticket': '兑换券',
    'auto_exchange_music_vocal_ticket': '翻唱自动兑换券',
    'birthday_party_delivery': '生日派对材料',
    'card_episode_release_ticket': '卡牌剧情解锁券',
}

INVENTORY_CATEGORY_ALIASES = {
    '水晶': 'jewel', '钻石': 'jewel', '石头': 'jewel',
    '火罐': 'boost', '演出能量': 'boost', '体力': 'boost',
    '记忆': 'memory', '回忆': 'memory', 'memory': 'memory',
}


# ================================ 背包分类参数与必要字段 ================================ #

def _parse_inventory_category(args: str, trigger_cmd: str) -> str:
    """解析分类参数；公共命令层提取账号参数后，背包命令仅允许自身账号序号。"""
    tokens = args.casefold().split()
    implicit_category = 'boost' if trigger_cmd == '/查火罐' else None
    if not tokens:
        return implicit_category or 'default'
    assert_and_reply(len(tokens) == 1, '一次只能查询一个背包分类')
    category = INVENTORY_CATEGORY_ALIASES.get(tokens[0])
    assert_and_reply(category is not None, f'无法解析的参数:"{args.strip()}"，可用分类：水晶、火罐、记忆')
    assert_and_reply(not implicit_category or category == implicit_category,
                     '/查火罐 只查询演出能量道具；其他分类请使用 /查背包')
    return category


def _get_inventory_required_fields(category: str) -> set[str]:
    """分类分别申请必要库存，避免查火罐时额外要求普通材料数据。"""
    return {
        'default': {'userMaterials'},
        'jewel': {'userChargedCurrency'},
        'boost': {'userBoostItems'},
        'memory': {'userMaterials', 'userMysekaiMaterials'},
    }[category]


def _get_inventory_sections(
    profile: dict, materials: list[dict], mysekai_materials: list[dict],
    category: str = 'default', boost_items: list[dict] | None = None,
) -> list[tuple]:
    """按同一份 Suite 库存与当前区服 MasterData 分区；零库存和未知材料也保留。"""
    if category == 'jewel':
        jewels = profile['userChargedCurrency']
        # 缺少免费/付费余额不是零余额，不能展示伪造的水晶数量。
        missing = [key for key in ('free', 'paid') if key not in jewels]
        assert_and_reply(not missing, f'抓包中缺少水晶余额字段：{", ".join(missing)}')
        return [('水晶', [
            {'name': name, 'description': '', 'quantity': jewels[key], 'seq': index,
             'path': 'thumbnail/common_material_rip/jewel.png'}
            for index, (name, key) in enumerate((('免费水晶', 'free'), ('付费水晶', 'paid')))
        ])]

    if category == 'boost':
        boost_map = {item['id']: item for item in boost_items or []}
        items = []
        for record in profile['userBoostItems']:
            item_id = record['boostItemId']
            item = boost_map.get(item_id, {})
            recovery = item.get('recoveryValue')
            items.append({
                'name': item.get('name', f'演出能量道具 #{item_id}'),
                'description': item.get('flavorText', f'恢复 {recovery} 演出能量' if recovery is not None else ''),
                'quantity': record['quantity'], 'seq': item.get('seq', item_id),
                'recovery_value': recovery,
                'expired_at': record.get('expiredAt', item.get('expiredAt')),
                'path': f'thumbnail/boost_item_rip/boost_item{item_id}.png',
            })
        return [('演出能量道具', sorted(items, key=lambda item: item['seq']))]

    material_map = {material['id']: material for material in materials}
    groups = {}
    memories = []
    for record in profile['userMaterials']:
        material_id = record['materialId']
        material = material_map.get(material_id, {})
        # 七色/七彩记忆是普通材料 168；角色记忆则属于烤森 game_character 材料。
        # 两者单独归入记忆，不按名称猜测，也不限制区服。
        is_memory = material_id == 168
        if is_memory != (category == 'memory'):
            continue
        section = material.get('materialType', 'other')
        if section not in INVENTORY_SECTION_NAMES:
            section = 'other'
        target = memories if is_memory else groups.setdefault(section, [])
        target.append({
            'name': material.get('name', f'材料 #{material_id}'),
            'description': material.get('flavorText', ''),
            'quantity': record['quantity'],
            'seq': material.get('seq', material_id),
            'expired_at': record.get('expiredAt', material.get('expiredAt')),
            'path': f'thumbnail/material_rip/material{material_id}.png',
        })
    labels = dict(INVENTORY_SECTION_NAMES, other='其他材料')
    sections = [(label, sorted(groups[key], key=lambda item: item['seq']))
                for key, label in labels.items() if key in groups]
    if category == 'default':
        # 只展示同份抓包确实提供的余额；部分精简抓包会省略虚拟币字段。
        game_data = profile.get('userGamedata', {})
        currencies = [
            {'name': name, 'description': '', 'quantity': game_data[key], 'seq': index,
             'path': f'thumbnail/common_material_rip/{resource}.png'}
            for index, (name, key, resource) in enumerate((('金币', 'coin', 'coin'), ('虚拟币', 'virtualCoin', 'virtual_coin')))
            if key in game_data
        ]
        if currencies:
            sections.insert(0, ('货币', currencies))

    # 旧抓包可能没有烤森库存；不从另一个抓包拼接，避免混用更新时间。
    mysekai_map = {material['id']: material for material in mysekai_materials}
    items = []
    for record in profile.get('userMysekaiMaterials', []):
        material_id = record['mysekaiMaterialId']
        material = mysekai_map.get(material_id, {})
        is_memory = material.get('mysekaiMaterialType') == 'game_character'
        if is_memory != (category == 'memory'):
            continue
        asset_name = material.get('iconAssetbundleName')
        target = memories if is_memory else items
        target.append({
            'name': material.get('name', f'烤森材料 #{material_id}'),
            'description': material.get('description', material.get('flavorText', '烤森')),
            'quantity': record['quantity'],
            'seq': material.get('seq', material_id),
            'path': f'mysekai/thumbnail/material/{asset_name}.png' if asset_name else None,
        })
    if category == 'memory':
        return [('记忆', sorted(memories, key=lambda item: item['seq']))]
    if items:
        sections.append(('烤森材料', sorted(items, key=lambda item: item['seq'])))
    return sections


def _get_inventory_boost_total(items: list[dict]) -> int | None:
    """按道具实际 recoveryValue 累加；未知的非零库存不能冒充完整合计。"""
    if any(item['quantity'] and item['recovery_value'] is None for item in items):
        return None
    return sum(item['quantity'] * (item['recovery_value'] or 0) for item in items)


# ================================ 背包有效期显示 ================================ #

def _get_inventory_expiry_text(item: dict, now: datetime) -> tuple[str, bool] | None:
    """统一以北京时间显示期限；空值或无效时间不影响整张背包图。"""
    if not item.get('expired_at'):
        return None
    # profile 依赖 resbox，沿用其秒/毫秒归一化时必须延迟导入。
    from .profile import to_unix_millis

    try:
        timestamp = to_unix_millis(item['expired_at'])
        if timestamp is None or timestamp <= 0:
            return None
        expires = datetime.fromtimestamp(timestamp / 1000, tz=now.tzinfo)
    except (ValueError, OverflowError, OSError):
        return None
    expired = expires <= now
    return ('已过期' if expired else f'有效期至 {expires:%m-%d %H:%M}', expired)


# ================================ 背包完整文字测量与四列布局 ================================ #

def _prepare_inventory_layouts(sections: list[tuple], style, now: datetime | None = None) -> list[tuple]:
    """按原字体测量完整文字，按每行最长内容确定卡片高度。"""
    # 同一页只取一次当前时间，避免临近到期时不同卡片的判断不一致。
    now = now or datetime.now(timezone(timedelta(hours=8)))
    # 面板宽 1180、左右内边距各 12、四列间距 10；余数分配到前两列，避免右侧额外留白。
    base_width, remainder = divmod(1180 - 12 * 2 - 10 * 3, 4)
    widths = [base_width + (column < remainder) for column in range(4)]
    layouts = []
    for label, items in sections:
        rows = []
        for start in range(0, len(items), 4):
            specs, row_height = [], 112
            for column, item in enumerate(items[start:start + 4]):
                width, text_width = widths[column], widths[column] - 16 - 58 - 6
                # 不设两行上限，不缩字号，不省略说明；沿用现有控件完整换行。
                name = TextBox(item['name'], style(17, bold=True), use_real_line_count=True,
                               line_sep=3, overflow='clip').set_w(text_width).set_padding(0)
                description_text = ' '.join(item['description'].split())
                description = None
                if description_text:
                    description = TextBox(description_text, style(12, auxiliary=True),
                                          use_real_line_count=True, line_sep=3,
                                          overflow='clip').set_w(text_width).set_padding(0)
                # ================================ 独立有效期与数量底栏 ================================ #
                quantity = TextBox(f"{item['quantity']:,}", style(16, bold=True)).set_padding(0)
                footer = quantity
                expiry = _get_inventory_expiry_text(item, now)
                if expiry:
                    expiry_text, expired = expiry
                    expiry_style = style(12, auxiliary=True)
                    if expired:
                        expiry_style = expiry_style.replace(color=RED)
                    # 有效期占左侧，数量仍在右下；底栏不参与正文居中计算。
                    expiry_width = width - 16 - quantity._get_self_size()[0] - 8
                    expiry_box = TextBox(expiry_text, expiry_style, use_real_line_count=True,
                                         line_sep=3, overflow='clip').set_w(expiry_width).set_padding(0)
                    footer = HSplit(items=[expiry_box, quantity]).set_w(width - 16).set_sep(8).set_item_align('b')
                text_height = name._get_self_size()[1]
                if description:
                    text_height += 12 + description._get_self_size()[1]
                # 正文以整卡中心为准；增高确保正文与独立数量至少隔开 8px。
                inner_height = max(58, text_height + 2 * (footer._get_self_size()[1] + 8))
                row_height = max(row_height, inner_height + 14)
                specs.append((item, width, text_width, name, description, footer))
            rows.append((row_height, specs))
        layouts.append((label, rows))
    return layouts


def _draw_inventory_rows(rows: list[tuple], icons: dict):
    """满行左右留白一致，同一行卡片等高，末行不足四项不拉伸。"""
    for row_height, specs in rows:
        with HSplit().set_w(1156).set_sep(10).set_content_align('lt').set_item_align('lt'):
            for item, width, text_width, name, description, footer in specs:
                inner_width, inner_height = width - 16, row_height - 14
                with Frame().set_size((width, row_height)).set_padding((8, 7)).set_bg(roundrect_bg()).set_content_align('lt'):
                    with HSplit().set_size((inner_width, inner_height)).set_sep(6).set_content_align('l').set_item_align('c'):
                        with Frame().set_size((58, 58)).set_content_align('c'):
                            ImageBox(icons[item['path']], size=(50, 50), image_size_mode='fit')
                        with VSplit().set_w(text_width).set_sep(12).set_content_align('lt').set_item_align('lt') as text_stack:
                            text_stack.add_item(name)
                            if description:
                                text_stack.add_item(description)
                    with Frame().set_size((inner_width, inner_height)).set_content_align('rb') as quantity_frame:
                        quantity_frame.add_item(footer)


# ================================ 背包渲染与命令 ================================ #

async def compose_inventory_image(ctx: SekaiHandlerContext, qid: int, category: str = 'default') -> Image.Image:
    """分类读取同一份 Suite，沿用资料栏与玻璃面板绘制背包。"""
    # profile 本身依赖 resbox，延迟导入以免形成模块初始化循环。
    from .profile import (
        get_detailed_profile, get_detailed_profile_card, get_detailed_profile_card_filter,
        format_suite_missing_fields_error,
    )

    required = get_detailed_profile_card_filter(*_get_inventory_required_fields(category))
    optional = {'userMysekaiMaterials'} if category == 'default' else set()
    profile, pmsg = await get_detailed_profile(
        ctx, qid, filter=required | optional, raise_exc=True, strict=False,
        require_verified_public=True,
    )
    missing = sorted(key for key in required if key not in profile)
    if missing:
        raise ReplyException(format_suite_missing_fields_error(ctx, profile, missing))
    materials = await ctx.md.materials.get() if category in ('default', 'memory') else []
    mysekai_materials = await ctx.md.mysekai_materials.get() if category in ('default', 'memory') and profile.get('userMysekaiMaterials') else []
    boosts = await ctx.md.boost_items.get() if category == 'boost' else []
    sections = _get_inventory_sections(profile, materials, mysekai_materials, category, boosts)

    async def load_icon(path):
        if not path:
            return UNKNOWN_IMG
        try:
            return await ctx.rip.img(path, use_img_cache=True)
        except Exception as exc:
            logger.warning(f'获取背包材料图标失败: {path}: {get_exc_desc(exc)}')
            return UNKNOWN_IMG

    paths = list(dict.fromkeys(item['path'] for _, items in sections for item in items))
    icons = dict(zip(paths, await batch_gather(*[load_icon(path) for path in paths])))

    def style(size, bold=False, auxiliary=False):
        return TextStyle(font=DEFAULT_BOLD_FONT if bold else DEFAULT_FONT, size=size,
                         color=(100, 100, 100) if auxiliary else BLACK)

    layouts = _prepare_inventory_layouts(sections, style)
    with Canvas(bg=SEKAI_BLUE_BG).set_padding(BG_PADDING) as canvas:
        with VSplit().set_sep(16).set_content_align('lt').set_item_align('lt'):
            await get_detailed_profile_card(ctx, profile, pmsg)
            with VSplit().set_w(1180).set_sep(14).set_content_align('lt').set_item_align('lt'):
                TextBox('背包一览', style(30, bold=True)).set_padding((8, 0))
                if category == 'boost':
                    total = _get_inventory_boost_total(sections[0][1])
                    text = f'合计可恢复演出能量：{total:,}' if total is not None else '存在未识别的道具，无法计算完整恢复量'
                    TextBox(text, style(22, bold=True)).set_padding((8, 0))
                if not any(items for _, items in sections):
                    TextBox('没有持有的道具', style(20)).set_padding(8)
                for label, rows in layouts:
                    if not rows:
                        continue
                    with VSplit().set_w(1180).set_padding(12).set_sep(10).set_bg(roundrect_bg()).set_content_align('lt').set_item_align('lt'):
                        with HSplit().set_sep(8).set_item_align('c'):
                            TextBox(label, style(22, bold=True)).set_padding(0)
                            TextBox(str(sum(len(specs) for _, specs in rows)), style(20, bold=True)).set_padding((8, 1))
                        _draw_inventory_rows(rows, icons)
    add_watermark(canvas)
    return await canvas.get_img()


pjsk_inventory = SekaiCmdHandler([
    '/查背包', '/背包一览', '/持有物', '/查持有物', '/inventory', '/pjsk inventory', '/查火罐',
])
pjsk_inventory.check_cdrate(cd).check_wblist(gbl)
@pjsk_inventory.handle()
async def _(ctx: SekaiHandlerContext):
    # 背包查询统一按发起者的绑定账号取数，u2 等序号用于切换自身账号。
    assert_and_reply(not ctx.uid_arg or ctx.uid_arg.startswith('u'),
                     '请查询自己绑定的账号，可用 u2 等序号切换')
    category = _parse_inventory_category(ctx.get_args(), ctx.trigger_cmd)
    await ctx.asend_reply_msg(await get_image_cq(
        await compose_inventory_image(ctx, ctx.user_id, category), low_quality=True,
    ))
