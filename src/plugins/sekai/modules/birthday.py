import copy
import math
import random
from PIL import ImageOps

from ...utils import *
from ..common import *
from ..handler import *
from ..asset import *
from ..draw import *
from .card import get_card_full_thumbnail, get_card_image, get_character_sd_image
from .honor import compose_full_honor_image
from .deck import make_deck_info_label
from .profile import (
    get_player_bind_id, get_basic_profile, get_detailed_profile,
    get_detailed_profile_card, get_detailed_profile_card_filter,
    get_player_avatar_info_by_detailed_profile, get_player_avatar_info_by_basic_profile,
    get_player_frames, get_selected_custom_avatar_frame, PlayerAvatarInfo,
)
from .mysekai import (
    get_mysekai_info, get_mysekai_info_card, get_mysekai_and_detail_profile_card,
)


# ================================ 参数与活动选择 ================================ #

# 浇水基础积分为每个露滴 100，生日卡 rate 与其相加；每朵门槛仍从本期表读取。
BIRTHDAY_DELIVERY_BASE_POINT = 100
BIRTHDAY_REWARD_PURPOSE = 'birthday_party_delivery_total_reward'
BIRTHDAY_PREPARE_CONCURRENCY = 4
BIRTHDAY_RESOURCE_TABLES = dict(
    material='materials', boost_item='boostItems', practice_ticket='practiceTickets',
    skill_practice_ticket='skillPracticeTickets', mysekai_material='mysekaiMaterials',
    mysekai_item='mysekaiItems', gacha_ticket='gachaTickets',
)


def _birthday_style(size=18, bold=False, color=(25, 25, 25, 255)):
    return TextStyle(DEFAULT_BOLD_FONT if bold else DEFAULT_FONT, size, color)


def _parse_birthday_args(args: str) -> tuple[int | None, int | None]:
    """解析角色简称与累计花朵目标；账号参数由 SekaiCmdHandler 提前处理。"""
    matches = list(re.finditer(r'目标\s*(\d+)', args))
    assert_and_reply(len(matches) <= 1, '一次只能指定一个花朵目标')
    target = int(matches[0].group(1)) if matches else None
    nickname = re.sub(r'目标\s*\d+', '', args).strip()
    if not matches:
        # 无“目标”时只接收末尾整数，u1/u2 等账号参数已由上层剥离。
        trailing = re.search(r'(?<![\d.+-])(\d+)\s*$', args)
        if trailing:
            target = int(trailing.group(1))
            nickname = args[:trailing.start()].strip()
    assert_and_reply(target is None or 0 < target <= 1000000, '花朵目标须为 1～1000000 的整数')
    cid = get_cid_by_nickname(nickname) if nickname else None
    assert_and_reply(not nickname or cid, '未识别到角色，请使用角色名或简称，例如 /生日浇水 hnm 目标25')
    return cid, target


async def _birthday_table(ctx: SekaiHandlerContext, name: str):
    """沿用 MasterData 管理器；未发布的可选表保留为空，不影响已可用的表。"""
    try:
        return await ctx.md.get(name)
    except Exception as exc:
        logger.warning(f'[{ctx.region}] 生日浇水读取 {name} 失败: {get_exc_desc(exc)}')
        return None


def _select_birthday_party(parties: list, cid: int | None, now: int, character_ids: dict | None = None) -> dict | None:
    """优先进行中的活动，其次下一场；指定角色时不借用该角色往年记录。"""
    character_ids = character_ids or {}
    candidates = [row for row in parties if cid is None
                  or character_ids.get(row['gameCharacterUnitId'], row['gameCharacterUnitId']) == cid]
    active = [row for row in candidates if row['startAt'] <= now <= row['closedAt']]
    assert_and_reply(cid is not None or len(active) <= 1,
                     '当前有多场生日浇水活动，请指定角色，例如 /生日浇水 hnm')
    if active:
        return max(active, key=lambda row: row['startAt'])
    upcoming = [row for row in candidates if row['startAt'] > now]
    return min(upcoming, key=lambda row: row['startAt']) if upcoming else None


def _matching_birthday_party(party: dict, candidates: list) -> dict | None:
    """CN 与 JP 存在进度差，按资源批次和角色匹配，不能按日历年份推算。"""
    matches = [row for row in candidates if row.get('assetbundleName') == party.get('assetbundleName')
               and row.get('gameCharacterUnitId') == party.get('gameCharacterUnitId')]
    return matches[0] if party.get('assetbundleName') and len(matches) == 1 else None


# ================================ 国服本轮规则参考 ================================ #

def _birthday_cycles(parties: list, character_ids: dict) -> list[list[dict]]:
    """按角色生日顺序分轮，只有前轮二十六角色齐全时才允许进入下一轮。"""
    cycles, current, seen = [], [], set()
    expected = set(range(1, 27))
    for party in sorted(parties, key=lambda row: (row['birthdayStartAt'], row['id'])):
        cid = character_ids.get(party['gameCharacterUnitId'])
        if cid not in expected:
            return []
        if cid in seen:
            # 历史缺项时，不能把下一轮的同角色记录拼进当前轮。
            if seen != expected:
                return []
            cycles.append(current)
            current, seen = [], set()
        current.append(party)
        seen.add(cid)
    if current:
        cycles.append(current)
    return cycles


# ================================ 同批次活动时间推算 ================================ #

def _birthday_reference_schedule(party: dict, anchors: list) -> dict | None:
    """从已公布活动校验日历年差与本地时刻，返回预计国服时间，冲突时保留未知。"""
    local_zone = timezone(timedelta(hours=REGION_UTC_OFFSET['cn']))
    peer_zone = timezone(timedelta(hours=REGION_UTC_OFFSET['jp']))
    shifts = {datetime.fromtimestamp(local['birthdayStartAt'] / 1000, local_zone).year
              - datetime.fromtimestamp(peer['birthdayStartAt'] / 1000, peer_zone).year for local, peer in anchors}
    if len(shifts) != 1:
        return None
    shift = shifts.pop()
    def project(stamp):
        """将日服时间转换为已验证年差下的国服本地时间。"""
        value = datetime.fromtimestamp(stamp / 1000, peer_zone)
        # 保留区服本地日历与时刻，不能把时区差或闰年当作固定秒数平移。
        return int(value.replace(year=value.year + shift, tzinfo=local_zone).timestamp() * 1000)
    keys = ('startAt', 'birthdayStartAt', 'closedAt')
    try:
        if any(project(peer[key]) != local[key] for local, peer in anchors for key in keys):
            return None
        return {key: project(party[key]) for key in keys}
    except ValueError:
        # 闰日跨到非闰年时没有唯一日历日期，不擅自调整到二月末。
        return None


async def _birthday_reference_party(ctx, parties, character_ids, cid, now):
    """定位国服本轮对应的日服活动，返回规则区服、活动和可推算的国服排期。"""
    if ctx.region != 'cn':
        return None
    local_cycles = _birthday_cycles(parties, character_ids)
    if not local_cycles:
        return None
    started = [rows for rows in local_cycles if min(row['startAt'] for row in rows) <= now]
    current = started[-1] if started else local_cycles[0]
    if any(character_ids[row['gameCharacterUnitId']] == cid for row in current):
        return None
    peer = SekaiHandlerContext.from_region('jp')
    peer_parties = await _birthday_table(peer, 'birthdayParties') or []
    peer_ids = {row['id']: row['gameCharacterId'] for row in await peer.md.game_character_units.get()}
    peer_cycles = _birthday_cycles(peer_parties, peer_ids)
    matched = [(row, _matching_birthday_party(row, peer_parties)) for row in current]
    matched = [(local, peer) for local, peer in matched if peer is not None]
    if not matched:
        return None
    anchors = {peer['id'] for _, peer in matched}
    candidates = [rows for rows in peer_cycles if anchors.issubset({row['id'] for row in rows})
                  and {peer_ids[row['gameCharacterUnitId']] for row in rows} == set(range(1, 27))]
    if len(candidates) != 1:
        return None
    target = [row for row in candidates[0] if peer_ids[row['gameCharacterUnitId']] == cid]
    return (peer, target[0], _birthday_reference_schedule(target[0], matched)) if len(target) == 1 else None


# ================================ 奖励数据与同批次预览 ================================ #

def _birthday_reward_bundle(rows, boxes, party_id: int):
    """校验本期奖励与资源箱的关联；缺资源箱时不将残缺数据当作完整奖励。"""
    selected = sorted((row for row in rows or [] if row.get('birthdayPartyId') == party_id),
                      key=lambda row: row['requirement'])
    if isinstance(boxes, list):
        boxes = {row['id']: row for row in boxes if row.get('resourceBoxPurpose') == BIRTHDAY_REWARD_PURPOSE}
    else:
        boxes = (boxes or {}).get(BIRTHDAY_REWARD_PURPOSE, {})
    if not selected or any(row['requirement'] <= 0 or not boxes.get(row['resourceBoxId'], {}).get('details') for row in selected):
        return None
    return selected, boxes


async def _birthday_rewards(ctx: SekaiHandlerContext, party: dict) -> dict:
    """先读本服奖励；CN 缺失时仅允许同一资源批次的 JP 数据作为带提示的预览。"""
    rows, boxes = await asyncio.gather(
        _birthday_table(ctx, 'birthdayPartyDeliveryTotalRewards'),
        _birthday_table(ctx, 'resourceBoxes'),
    )
    local = _birthday_reward_bundle(rows, boxes, party['id'])
    result = dict(ctx=ctx, rows=[], boxes={}, preview=False, peer=None)
    if local:
        result.update(rows=local[0], boxes=local[1])
        return result
    if ctx.region != 'cn':
        return result
    peer = SekaiHandlerContext.from_region('jp')
    peer_party = _matching_birthday_party(party, await _birthday_table(peer, 'birthdayParties') or [])
    if peer_party is None:
        return result
    peer_rows, peer_boxes = await asyncio.gather(
        _birthday_table(peer, 'birthdayPartyDeliveryTotalRewards'),
        _birthday_table(peer, 'resourceBoxes'),
    )
    bundle = _birthday_reward_bundle(peer_rows, peer_boxes, peer_party['id'])
    if bundle:
        # 已发布的 CN 档位必须在对应批次中存在，不能悄悄覆盖已知本服规则。
        known = {row['requirement'] for row in rows or [] if row.get('birthdayPartyId') == party['id']}
        if not known.issubset({row['requirement'] for row in bundle[0]}):
            return result
        local_boxes = boxes if isinstance(boxes, dict) else {BIRTHDAY_REWARD_PURPOSE: {
            row['id']: row for row in boxes or [] if row.get('resourceBoxPurpose') == BIRTHDAY_REWARD_PURPOSE}}
        local_boxes = local_boxes.get(BIRTHDAY_REWARD_PURPOSE, {})
        peer_by_requirement = {row['requirement']: bundle[1][row['resourceBoxId']]['details'] for row in bundle[0]}
        signature = lambda items: sorted((item['resourceType'], item.get('resourceId', 0),
                                          item['resourceQuantity'], item.get('resourceLevel', 0)) for item in items)
        for row in rows or []:
            if row.get('birthdayPartyId') != party['id']:
                continue
            details = local_boxes.get(row['resourceBoxId'], {}).get('details')
            if details and signature(details) != signature(peer_by_requirement[row['requirement']]):
                return result
        result.update(ctx=peer, rows=bundle[0], boxes=bundle[1], preview=True, peer=peer)
    return result


def _birthday_find(data: dict, table: str, key: str, value):
    rows = data.get(table)
    return next((row for row in rows if row.get(key) == value), None) if isinstance(rows, list) else None


def _birthday_calculate(party, bonuses, threshold, target, suite, capture, prestart, top):
    """分别提取持卡、库存与当期进度，缺字段保持未知，积分向上取整为露滴需求。"""
    resources = capture.get('updatedResources', {}) if capture else {}
    cards = suite.get('userCards') if suite else None
    owned = {row['cardId'] for row in cards} if isinstance(cards, list) else None
    bonus = sum(row['rate'] for row in bonuses if top or row['cardId'] in owned) if bonuses and (top or owned is not None) else None
    # 生日累计进度来自 MySekai，Suite 只提供持卡与库存。
    progress = _birthday_find(resources, 'userBirthdayParties', 'birthdayPartyId', party['id'])
    planning = prestart or top
    count = 0 if planning else progress.get('obtainedMysekaiMaterialCount') if progress else None
    point = 0 if planning else progress.get('deliveryTotalPoint') if progress else None
    inventory = _birthday_find(suite or {}, 'userMaterials', 'materialId', party['deliveryItemMaterialId'])
    flowers = _birthday_find(suite or {}, 'userMysekaiMaterials', 'mysekaiMaterialId', party['deliveryRewardMysekaiMaterialId'])
    inventory = inventory.get('quantity') if inventory else None
    flowers = flowers.get('quantity') if flowers else None
    per_dew = BIRTHDAY_DELIVERY_BASE_POINT + bonus if bonus is not None else None
    remaining = max(0, target * threshold - point) if target is not None and threshold and point is not None else None
    divisor = per_dew or BIRTHDAY_DELIVERY_BASE_POINT
    need = (remaining + divisor - 1) // divisor if remaining is not None else None
    upper_bound = need is not None and per_dew is None
    gap = max(0, need - inventory) if need is not None and inventory is not None and not upper_bound else None
    return dict(owned=owned, bonus=bonus, count=count, point=point, inventory=inventory,
                flower_stock=flowers, per_dew=per_dew, need=need, gap=gap, upper_bound=upper_bound,
                planning=planning, target=target, threshold=threshold, top=top, bonuses=bonuses)


async def _birthday_account(ctx: SekaiHandlerContext, uid: str | None):
    """访问资格先由绑定入口确认；两份数据独立获取，不绕过隐藏、授权及账号序号规则。"""
    if not uid:
        return None, None, None, None
    qid = int(ctx.uid_arg[1:]) if (ctx.uid_arg or '').startswith('@') else ctx.user_id
    fields = get_detailed_profile_card_filter('userMaterials', 'userMysekaiMaterials')
    (suite, _), (capture, _) = await asyncio.gather(
        get_detailed_profile(ctx, qid, filter=fields, strict=False, mode=ctx.data_mode_arg, require_verified_public=True),
        get_mysekai_info(ctx, qid, mode=ctx.data_mode_arg),
    )
    suite_uid = (suite or {}).get('userGamedata', {}).get('userId')
    resources = (capture or {}).get('updatedResources', {})
    capture_uid = resources.get('userMysekaiGamedata', {}).get('userId')
    assert_and_reply(suite_uid is None or str(suite_uid) == str(uid), 'Suite 数据与当前选择的游戏账号不一致，请重新上传')
    assert_and_reply(capture_uid is None or str(capture_uid) == str(uid), 'MySekai 数据与当前选择的游戏账号不一致，请重新上传')
    if not suite and not capture:
        return None, None, None, None

    # ================================ 头像资料回退 ================================ #
    # 计算仍使用原Suite；只有头像缺资料时补查个人资料，不向持卡字段填入替代数据。
    basic, avatar = None, None
    if suite:
        try:
            avatar = await get_player_avatar_info_by_detailed_profile(ctx, suite)
        except Exception as exc:
            logger.warning(f'生日浇水 Suite 头像暂不可用，尝试个人资料: {get_exc_desc(exc)}')
    if avatar is None:
        try:
            basic = await get_basic_profile(ctx, uid, raise_when_no_found=False)
        except Exception as exc:
            logger.warning(f'生日浇水基本资料读取失败，保留抓包进度: {get_exc_desc(exc)}')
        basic_uid = (basic or {}).get('user', {}).get('userId')
        assert_and_reply(basic_uid is None or str(basic_uid) == str(uid), '个人资料与当前选择的游戏账号不一致，请稍后重试')
        if basic:
            try:
                avatar = await get_player_avatar_info_by_basic_profile(ctx, basic)
            except Exception as exc:
                logger.warning(f'生日浇水个人头像暂不可用，使用默认头像: {get_exc_desc(exc)}')
    if avatar is None:
        avatar = PlayerAvatarInfo(0, 0, 'piapro', UNKNOWN_IMG)
    return suite, capture, basic, avatar


# ================================ 账号与活动规则并行准备 ================================ #

async def _birthday_gather(*coroutines):
    """并行准备相互独立的数据，失败或取消时回收本组剩余任务。"""
    tasks = [asyncio.create_task(coroutine) for coroutine in coroutines]
    try:
        return await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


async def _birthday_rules(ctx: SekaiHandlerContext, party: dict):
    """并行读取奖励、卡牌加成和花朵门槛表。"""
    rewards, bonus_rows, delivery_rows = await asyncio.gather(
        _birthday_rewards(ctx, party),
        _birthday_table(ctx, 'birthdayPartyDeliveryPointBonuses'),
        _birthday_table(ctx, 'birthdayPartyDeliveryRewards'),
    )
    return rewards, bonus_rows, delivery_rows


async def _birthday_prepare(ctx: SekaiHandlerContext, uid: str | None, party: dict, rules_ctx=None):
    """并行准备账号和规则；失败或取消时回收两条分支，保留原始异常。"""
    account, rules = await _birthday_gather(_birthday_account(ctx, uid), _birthday_rules(rules_ctx or ctx, party))
    return (*account, *rules)


# ================================ 资源读取与请求内复用 ================================ #

async def _birthday_prepare_tables(data, material_ctx):
    """并发准备本页实际使用的表，后续资源读取继续使用原缓存和回退规则。"""
    ctx, rewards = data['ctx'], data['rewards']
    contexts = {ctx.region: ctx, material_ctx.region: material_ctx}
    requests = {(material_ctx.region, 'mysekaiMaterials')}
    honor_ctx = rewards['ctx'] if data.get('reference') else ctx
    for row in rewards['rows']:
        if row['requirement'] > data['state']['target']:
            continue
        for item in rewards['boxes'][row['resourceBoxId']]['details']:
            kind = item['resourceType']
            if kind == 'honor':
                contexts[honor_ctx.region] = honor_ctx
                requests.update((honor_ctx.region, name) for name in ('honors', 'honorGroups'))
            elif table := BIRTHDAY_RESOURCE_TABLES.get(kind):
                requests.add((ctx.region, table))
    if data['suite'] or data['capture']:
        frames = get_player_frames(ctx, data['uid'], data['suite'])
        if any(frame.get('playerFrameAttachStatus') == 'first' for frame in frames) and not get_selected_custom_avatar_frame(ctx.region, data['uid']):
            requests.update((ctx.region, name) for name in ('playerFrames', 'playerFrameGroups'))
    # 限制单次查询的预备请求数，组件仍按原顺序创建，避免异步任务交错进入绘图容器。
    limiter = asyncio.Semaphore(BIRTHDAY_PREPARE_CONCURRENCY)

    async def prepare(region, name):
        async with limiter:
            await _birthday_table(contexts[region], name)

    await asyncio.gather(*(prepare(region, name) for region, name in sorted(requests)))


class _BirthdayRipView:
    """称号合成沿用原函数，缺图时按相同资源路径补查 JP，失败保留文字档位。"""
    def __init__(self, ctx, cache):
        self.ctx, self.cache = ctx, cache

    async def img(self, path, **kwargs):
        key = (self.ctx.region, path)
        if key not in self.cache:
            picture = await self.ctx.rip.img(path, **{**kwargs, 'allow_error': True, 'default': None})
            if picture is None and self.ctx.region == 'cn':
                picture = await SekaiHandlerContext.from_region('jp').rip.img(
                    path, **{**kwargs, 'allow_error': True, 'default': None})
            self.cache[key] = picture
        picture = self.cache[key]
        if picture is None and not kwargs.get('allow_error', False):
            raise FileNotFoundError(path)
        return picture if picture is not None else kwargs.get('default')


async def _birthday_icon(data, ctx, path):
    """使用统一解包资源管理器的缓存和主备源，单次查询内复用成功或失败结果。"""
    picture = await _BirthdayRipView(ctx, data['images']).img(path, allow_error=True)
    return picture if picture is not None else UNKNOWN_IMG


async def _birthday_honor(data, item):
    """按资源箱指定的称号等级合成；本服缺元数据时补查同 ID，完全缺失返回空图。"""
    ctx = data['rewards']['ctx'] if data.get('reference') else data['ctx']
    key = ('honor', ctx.region, item['resourceId'], item['resourceLevel'])
    if key in data['images']:
        return data['images'][key]
    try:
        honor = await ctx.md.honors.find_by_id(item['resourceId'])
        if honor is None and data['rewards']['preview']:
            ctx = data['rewards']['ctx']
            honor = await ctx.md.honors.find_by_id(item['resourceId'])
        if honor is None and ctx.region == 'cn':
            peer = SekaiHandlerContext.from_region('jp')
            peer_party = _matching_birthday_party(data['party'], await _birthday_table(peer, 'birthdayParties') or [])
            if peer_party:
                peer_rows, peer_boxes = await asyncio.gather(
                    _birthday_table(peer, 'birthdayPartyDeliveryTotalRewards'), _birthday_table(peer, 'resourceBoxes'))
                bundle = _birthday_reward_bundle(peer_rows, peer_boxes, peer_party['id'])
                same_honor = bundle and any(detail.get('resourceType') == 'honor'
                    and detail.get('resourceId') == item['resourceId']
                    and detail.get('resourceLevel') == item['resourceLevel']
                    for row in bundle[0] for detail in bundle[1][row['resourceBoxId']]['details'])
                if same_honor:
                    ctx = peer
                    honor = await ctx.md.honors.find_by_id(item['resourceId'])
        if honor is None:
            data['images'][key] = None
            return None
        view = copy.copy(ctx)
        view.rip = _BirthdayRipView(ctx, data['images'])
        picture = await compose_full_honor_image(view, dict(
            honorId=item['resourceId'], honorLevel=item['resourceLevel'], profileHonorType='normal'), True)
    except Exception as exc:
        logger.warning(f'生日称号 {item["resourceId"]} 素材暂不可用: {get_exc_desc(exc)}')
        picture = None
    data['images'][key] = picture
    return picture


async def _birthday_resource(data, kind, resource_id):
    """优先本服名称与图标，预览奖励缺少本服元数据时读取对应批次的区服。"""
    key = (kind, resource_id)
    if key in data['resources']:
        return data['resources'][key]
    ctx = data['ctx']
    table = BIRTHDAY_RESOURCE_TABLES.get(kind)
    row = {}
    contexts = [ctx]
    if data['rewards']['ctx'].region != ctx.region:
        contexts.append(data['rewards']['ctx'])
    for candidate in contexts:
        metadata_key = (candidate.region, table)
        if table and metadata_key not in data['metadata']:
            data['metadata'][metadata_key] = {entry['id']: entry for entry in await _birthday_table(candidate, table) or []}
        row = data['metadata'].get(metadata_key, {}).get(resource_id, {})
        if row:
            ctx = candidate
            break
    paths = dict(
        material=f'thumbnail/material_rip/material{resource_id}.png',
        boost_item=f'thumbnail/boost_item_rip/boost_item{resource_id}.png',
        practice_ticket=f'thumbnail/practice_ticket_rip/ticket{resource_id}.png',
        skill_practice_ticket=f'thumbnail/skill_practice_ticket_rip/ticket{resource_id}.png',
        gacha_ticket=f'thumbnail/gacha_ticket/{row.get("assetbundleName", "gacha_ticket")}.png',
        mysekai_material=f'mysekai/thumbnail/material/{row.get("iconAssetbundleName", "")}.png',
        mysekai_item=f'mysekai/thumbnail/item/{row.get("iconAssetbundleName", "")}.png',
    )
    picture = await _birthday_icon(data, ctx, paths[kind]) if kind in paths and row else UNKNOWN_IMG
    result = row.get('name', f'道具 #{resource_id}'), picture
    data['resources'][key] = result
    return result


# ================================ 资料栏与活动排期 ================================ #

def _fit_birthday_profile_card(card, width=512):
    """按生日页栏宽调整传入资料卡，保留头像和文字样式，长文字自动换行。"""
    row = card.items[0]
    text_stack = next(item for item in row.items if isinstance(item, VSplit))
    text_width = (width - card.hpadding * 2 - row.hpadding * 2
                  - row.sep * (len(row.items) - 1)
                  - sum(item._get_self_size()[0] for item in row.items if item is not text_stack))
    card.set_w(width)
    text_stack.set_w(text_width)

    def wrap_text(text, available_width):
        text.set_w(available_width).set_wrap(True)
        text.use_real_line_count = True
        text.line_count = 99999

    items = list(text_stack.items)
    for index, item in enumerate(items):
        if isinstance(item, TextBox):
            wrap_text(item, text_width)
        elif isinstance(item, HSplit) and item._get_self_size()[0] > text_width:
            # 彩色昵称由多个文字片段组成，保留各段颜色并在栏宽内排列。
            segments, pending = [], list(item.items)
            while pending:
                child = pending.pop(0)
                if isinstance(child, TextBox):
                    segments.append(child)
                else:
                    pending[0:0] = child.items
            with text_stack:
                with Flow().set_w(text_width).set_padding((item.hpadding, item.vpadding)).set_sep(0, 2).set_content_and_item_align('l') as wrapped:
                    for segment in segments:
                        segment_width = min(segment._get_self_size()[0], text_width - item.hpadding * 2)
                        wrap_text(segment, segment_width)
                        wrapped.add_item(segment)
            items[index] = wrapped
    text_stack.set_items(items)
    # 资料栏可能已被测量过，文字换行后重新计算所有子项的尺寸。
    pending = [card]
    while pending:
        widget = pending.pop()
        widget._calc_w = widget._calc_h = None
        pending.extend(getattr(widget, 'items', []))


async def _draw_birthday_profile(data):
    """复用原资料栏；缺失提示加在栏内，生日页不展示 MySekai 等级。"""
    ctx, suite, capture = data['ctx'], data['suite'], data['capture']
    with VSplit().set_w(512).set_item_align('l'):
        if not suite and not capture:
            with VSplit().set_w(512).set_padding(16).set_sep(10).set_bg(roundrect_bg()).set_item_align('l'):
                TextBox(f'{get_region_name(ctx.region)}满配', _birthday_style(26, True))
                TextBox('*未绑定账号，按满配估算' if not data['uid'] else '*未获取到 Suite 和 MySekai 数据，按满配估算', _birthday_style(17, color=RED))
                TextBox('全部加成卡，从零计算总需求', _birthday_style(16))
            return
        # 资料栏的姓名和头像允许使用个人资料，库存与持卡计算始终保留原抓包。
        game_data = {**(data['basic'] or {}).get('user', {}), **(suite or {}).get('userGamedata', {})}
        game_data.update(userId=data['uid'], name=game_data.get('name') or '未知玩家')
        display_suite = {**suite, 'userGamedata': game_data} if suite else None
        display_basic = {**(data['basic'] or {}), 'user': game_data}
        if suite and capture:
            card = await get_mysekai_and_detail_profile_card(ctx, capture, display_suite, '', show_level=False, avatar_info=data['avatar'])
        elif suite:
            card = await get_detailed_profile_card(ctx, display_suite, '', avatar_info=data['avatar'])
        else:
            card = await get_mysekai_info_card(ctx, capture, display_basic, '', show_level=False, avatar_info=data['avatar'])
        if not suite or not capture:
            text_stack = next(item for item in card.items[0].items if isinstance(item, VSplit))
            with text_stack:
                TextBox('*未获取到 Suite 数据' if not suite else '*未获取到 MySekai 数据', _birthday_style(16, color=RED))
        _fit_birthday_profile_card(card)


def _draw_birthday_schedule(label, start, end, now, color):
    """日期按原活动时间显示，进度使用角色应援原色。"""
    ratio = max(0, min(1, (now - start) / (end - start)))
    date_text = lambda stamp: datetime.fromtimestamp(stamp / 1000).strftime('%m/%d %H:%M')
    with HSplit().set_sep(8).set_item_align('c'):
        TextBox(label, _birthday_style(16)).set_w(72)
        with Frame().set_w(142).set_h(10):
            Spacer(w=142, h=10).set_bg(RoundRectBg((75, 75, 75, 255), 4))
            if ratio:
                Spacer(w=max(1, int(140 * ratio)), h=8).set_bg(RoundRectBg(color, 4)).set_offset((1, 1))
        TextBox(f'{date_text(start)} — {date_text(end - 1000)}', _birthday_style(14)).set_w(250).set_content_align('r')


async def _draw_birthday_header(data):
    ctx, party, cid = data['ctx'], data['party'], data['cid']
    with VSplit().set_w(512).set_padding(12).set_sep(8).set_bg(roundrect_bg()).set_content_and_item_align('c'):
        with HSplit().set_w(488).set_sep(12).set_item_align('c').set_content_align('c'):
            # 与“/生日”一致使用角色基础SD，活动关联单元不改变生日资料栏的形象。
            ImageBox(await get_character_sd_image(cid), size=(None, 46), shadow=True)
            label = await _birthday_icon(data, SekaiHandlerContext.from_region('jp'), f'character/label_horizontal/chr_h_lb_{cid}.png')
            ImageBox(label, size=(None, 32))
            month, day = get_character_birthday(cid)
            TextBox(f'{month}月{day}日', _birthday_style(24, True))
        reference = data.get('reference', False)
        schedule = data.get('reference_schedule') if reference else party
        if schedule:
            year = datetime.fromtimestamp(schedule['birthdayStartAt'] / 1000).year
            now = data['now']
            mode = '进行中'
            if now < schedule['startAt']:
                seconds = (schedule['startAt'] - now) / 1000
                mode = f'距开始还有{int(seconds // 86400)}天' if seconds >= 86400 else f'距开始还有{max(1, math.ceil(seconds / 3600))}小时'
            elif now > schedule['closedAt']:
                mode = '预计已结束' if reference else '已结束'
            with HSplit().set_w(488).set_sep(14).set_item_align('c').set_content_align('c'):
                TextBox(f"{year} 年生日浇水{'预览' if reference else ''}", _birthday_style(21, True))
                TextBox(mode, _birthday_style(16))
            _draw_birthday_schedule('露滴掉落', schedule['startAt'], schedule['birthdayStartAt'], now, data['color'])
            _draw_birthday_schedule('浇水开放', schedule['startAt'], schedule['closedAt'] + 1000, now, data['color'])
            if reference:
                TextBox('*时间为推算，规则参考日服', _birthday_style(16, color=RED))
        elif reference:
            TextBox('国服本轮生日浇水预览', _birthday_style(21, True))
            TextBox('*活动时间暂无法推算', _birthday_style(16, color=RED))
            TextBox('*按日服对应轮次规则预览', _birthday_style(16, color=RED))
        else:
            TextBox(f'{get_region_name(ctx.region)}生日浇水', _birthday_style(21, True))


# ================================ 花朵进度与露滴需求 ================================ #

def _draw_birthday_progress(value, total):
    """沿用项目分段色，刻度贯穿内槽；零进度同样保留刻度。"""
    ratio = max(0, min(1, value / total))
    colors = [(255, 50, 50), (255, 100, 100), (255, 150, 100), (255, 200, 100), (255, 255, 100)]
    color = (100, 255, 100) if ratio >= 1 else colors[min(4, max(0, math.ceil(ratio * 5) - 1))]
    color = (*color, 255)
    with Frame().set_w(480).set_h(18):
        Spacer(w=480, h=14).set_bg(RoundRectBg((100, 100, 100, 255), 7))
        if ratio:
            Spacer(w=max(1, int(476 * ratio)), h=10).set_bg(RoundRectBg(color, 6)).set_offset((2, 2))
        for index in range(1, 5):
            Spacer(w=1, h=10).set_bg(FillBg((100, 100, 100, 255) if index / 5 < ratio else (150, 150, 150, 255))).set_offset((2 + int(476 * index / 5), 2))


def _draw_birthday_progress_panel(data, flower, dew, is_flower):
    state = data['state']
    point, threshold = state['point'], state['threshold']
    value = state['count'] if is_flower else point % threshold if point is not None and threshold else None
    total = state['target'] if is_flower else threshold
    # 积分与累计获得数分别上报；已够下一朵时保留满条，不因取余显示成重新开始。
    pending_flower = point is not None and threshold and state['count'] is not None and point >= (state['count'] + 1) * threshold
    if not is_flower and pending_flower:
        value = threshold
    with VSplit().set_w(512).set_padding(16).set_sep(10).set_item_align('l'):
        with HSplit().set_sep(8).set_item_align('c'):
            ImageBox(flower if is_flower else dew, size=(36, 36))
            TextBox(('起算花朵' if state['planning'] else '累计花朵') if is_flower else '下一朵浇水积分', _birthday_style(21, True))
        if value is not None and total:
            _draw_birthday_progress(value, total)
            with Frame().set_w(480):
                TextBox(f'{value:,}/{total:,} ({value / total:.1%})', _birthday_style(15))
                if is_flower and state['flower_stock'] is not None:
                    TextBox(f"库存 {state['flower_stock']} 朵", _birthday_style(15)).set_w(480).set_content_align('r')
            if is_flower:
                TextBox(f"{'起算值' if state['planning'] else '已获得'} {value} 朵　目标 {total} 朵", _birthday_style(16))
            elif pending_flower:
                TextBox('积分已满足下一朵，累计花朵记录尚未增加', _birthday_style(16))
            elif state['per_dew']:
                TextBox(f"再消耗 {math.ceil((total - value) / state['per_dew'])} 个露滴可培育下一朵", _birthday_style(16))
            else:
                TextBox(f'距下一朵还差 {total - value:,} 积分', _birthday_style(16))
        else:
            TextBox('*本期花朵目标暂未公布' if is_flower and value is not None else '*未获取到生日进度或积分规则', _birthday_style(16, color=RED))
            if is_flower and state['flower_stock'] is not None:
                TextBox(f"现有花朵 {state['flower_stock']} 朵", _birthday_style(16))
            elif is_flower and value is not None:
                TextBox(f'累计获得 {value} 朵', _birthday_style(16))


def _draw_birthday_requirement(data, dew):
    state = data['state']
    quantity = lambda value: f'{value:,} 个' if value is not None else '未知'
    with VSplit().set_w(1040).set_padding(18).set_sep(15).set_bg(roundrect_bg()).set_item_align('l'):
        TextBox('露滴需求', _birthday_style(21, True))
        metrics = [('从零所需露滴', quantity(state['need'])), ('目标花朵', f"{state['target']} 朵" if state['target'] is not None else '未知'),
                   ('每个露滴获得', f"{state['per_dew']} 积分" if state['per_dew'] else '未知')] if state['top'] else [
                       ('最多所需露滴' if state['upper_bound'] else '所需露滴', quantity(state['need'])),
                       ('现有露滴', quantity(state['inventory'])), ('露滴缺口', quantity(state['gap']))]
        with HSplit().set_sep(14).set_item_align('c'):
            ImageBox(dew, size=(64, 64))
            for index, (label, value) in enumerate(metrics):
                with VSplit().set_w(294).set_sep(10).set_content_align('c').set_item_align('c'):
                    TextBox(label, _birthday_style(18))
                    TextBox(value, _birthday_style(30 if value != '未知' else 23, True, RED if index == 2 and state['gap'] else (25, 25, 25)))
        note = None
        if state['need'] is None:
            if state['point'] is None and state['target'] and state['threshold'] and state['per_dew']:
                need = math.ceil(state['target'] * state['threshold'] / state['per_dew'])
                note = f"*未获取到生日进度，从零获得 {state['target']} 朵共需 {need:,} 个露滴"
            elif state['target'] is None and state['threshold']:
                note = f"*最高奖励目标暂未公布，可指定花朵目标，例如 /生日浇水 {get_character_first_nickname(data['cid'])} 目标25"
            elif state['threshold'] is None:
                note = '*本期浇水积分规则暂未公布，暂无法计算露滴需求'
        elif state['upper_bound']:
            note = '*未获取到生日卡持有数据，按无加成估算最多需求'
        elif data.get('reference'):
            note = '*按日服对应轮次规则，以满配从零估算露滴需求' if state['top'] else '*按日服对应轮次规则，按当前持卡从零计算露滴需求'
        elif data['prestart']:
            note = '*活动尚未开始，按满配从零估算露滴需求' if state['top'] else '*活动尚未开始，按当前持卡从零计算露滴需求'
        if note:
            TextBox(note, _birthday_style(16, color=RED))


async def _draw_birthday_cards(data, birthday_cards):
    """每行最多六张且末行居中，缺 Suite 保留卡面并标注持有未知。"""
    ctx, state = data['ctx'], data['state']
    pictures = []
    for bonus in state['bonuses']:
        card = await ctx.md.cards.find_by_id(bonus['cardId'])
        card_ctx = ctx
        if card is None:
            card_ctx = SekaiHandlerContext.from_region('jp')
            card = await card_ctx.md.cards.find_by_id(bonus['cardId'])
        picture = await get_card_full_thumbnail(card_ctx, card, False) if card else UNKNOWN_IMG
        held = state['owned'] is not None and bonus['cardId'] in state['owned']
        if not held and not state['top']:
            picture = ImageOps.grayscale(picture).convert('RGBA')
        pictures.append((bonus, picture, held, card and (card_ctx.region != ctx.region or card.get('releaseAt', 0) > data['now'])))
    with VSplit().set_w(1040).set_padding(18).set_sep(16).set_bg(roundrect_bg()).set_item_align('l'):
        with HSplit().set_sep(24).set_item_align('c'):
            TextBox('生日卡加成', _birthday_style(21, True))
            TextBox(f"积分加成 +{state['bonus']}%　每个露滴 {state['per_dew']} 积分" if state['bonus'] is not None else '*未获取到持卡数据或本期加成规则', _birthday_style(17, color=(55, 55, 55) if state['bonus'] is not None else (125, 125, 125)))
        if not pictures:
            TextBox('*本期生日卡加成规则暂未公布', _birthday_style(16, color=RED))
            return
        rows = (len(pictures) + 5) // 6
        columns = (len(pictures) + rows - 1) // rows
        cell_width = (1004 - 16 * (columns - 1)) // columns
        for start in range(0, len(pictures), columns):
            with Frame().set_w(1004).set_content_align('c'):
                with HSplit().set_sep(16).set_item_align('t'):
                    for bonus, picture, held, leak in pictures[start:start + columns]:
                        unknown = state['owned'] is None and not state['top']
                        color = (125, 125, 125) if unknown else (25, 25, 25)
                        with VSplit().set_w(cell_width).set_sep(7).set_content_align('c').set_item_align('c'):
                            with Frame().set_w(128).set_h(128).set_content_align('lb'):
                                ImageBox(picture, size=(128, 128))
                                if leak:
                                    TextBox('LEAK', _birthday_style(20, True, RED)).set_offset((4, -4))
                            TextBox(f"+{bonus['rate']}%", _birthday_style(20, True, color))
                            status_color = ((50, 50, 50) if state['top'] else (125, 125, 125) if unknown
                                            else (50, 150, 50) if held else (150, 50, 50))
                            make_deck_info_label(
                                '满配计入' if state['top'] else '持有未知' if unknown else '已持有' if held else '未持有',
                                status_color, size=16,
                            ).set_padding(2).set_bg(RoundRectBg((255, 255, 255, 150), 2))


# ================================ 累计奖励与生日称号 ================================ #

def _draw_birthday_reward_track(current, total, color):
    with Frame().set_w(160).set_h(8):
        Spacer(w=160, h=8).set_bg(RoundRectBg((80, 90, 105, 255), 4))
        if current:
            Spacer(w=max(2, int(160 * min(1, current / total))), h=8).set_bg(RoundRectBg(color, 4))


async def _draw_birthday_next_reward(data, row):
    """普通道具在前、称号在后，续行对齐第一项奖励。"""
    state, rewards = data['state'], data['rewards']
    details = sorted(rewards['boxes'][row['resourceBoxId']]['details'], key=lambda item: item['resourceType'] == 'honor')
    with HSplit().set_w(1004).set_sep(14).set_item_align('lt'):
        label = TextBox('下一档奖励', _birthday_style(17, True)).set_h(32).set_content_align('l')
        with Flow().set_w(1004 - label._get_self_size()[0] - 14).set_sep(14, 8).set_item_align('l').set_content_align('l'):
            for item in details:
                with HSplit().set_sep(6).set_item_align('c'):
                    if item['resourceType'] == 'honor':
                        picture = await _birthday_honor(data, item)
                        if picture is not None:
                            ImageBox(picture, size=(150, None))
                        name = '生日称号'
                    else:
                        name, picture = await _birthday_resource(data, item['resourceType'], item['resourceId'])
                        ImageBox(picture, size=(32, 32))
                    TextBox(f"{name}×{item['resourceQuantity']}", _birthday_style(17))
            if state['count'] is not None and state['point'] is not None and state['threshold']:
                points = max(0, row['requirement'] * state['threshold'] - state['point'])
                if points:
                    TextBox(f"还差 {max(0, row['requirement'] - state['count'])} 朵（{points:,} 积分）", _birthday_style(17))
                else:
                    TextBox('积分已满足目标，累计花朵记录尚未达到该档位', _birthday_style(17))


def _birthday_footer_content(data, honors):
    """按查询目标生成指令与说明，目标内没有称号时仍能返回完整查询。"""
    state, rewards = data['state'], data['rewards']
    alias = get_character_first_nickname(data['cid'])
    maximum = max(row['requirement'] for row in rewards['rows'])
    if state['target'] < maximum:
        return f'/生日浇水 {alias}', '计算全部奖励所需露滴'
    next_honor = next((flowers for flowers, _ in honors if state['count'] is not None and flowers > state['count']), None)
    if next_honor:
        return f'/生日浇水 {alias} {next_honor}', f'计算 {next_honor} 朵称号所需露滴'
    return None


def _draw_birthday_footer(data, honors):
    """绘制目标查询引导，使用与正文一致的字号。"""
    if content := _birthday_footer_content(data, honors):
        command, description = content
        Spacer(w=1004, h=1).set_bg(FillBg((150, 170, 185, 255)))
        with HSplit().set_sep(8).set_item_align('c'):
            TextBox('发送', _birthday_style(21))
            make_deck_info_label(command, size=21).set_padding((6, 4)).set_bg(
                RoundRectBg((255, 255, 255, 150), 2))
            TextBox(description, _birthday_style(21))


async def _draw_birthday_rewards(data):
    """奖励与称号按同一目标裁剪，未知进度不生成零库存或已解锁状态。"""
    state, rewards = data['state'], data['rewards']
    if not rewards['rows']:
        with VSplit().set_w(1040).set_padding(18).set_bg(roundrect_bg()):
            TextBox('*本期累计奖励与生日称号数据暂未公布', _birthday_style(17, color=RED))
        return
    rows = [row for row in rewards['rows'] if row['requirement'] <= state['target']]
    totals, honors = {}, []
    for row in rows:
        for item in rewards['boxes'][row['resourceBoxId']]['details']:
            if item['resourceType'] == 'honor':
                honors.append((row['requirement'], item))
            else:
                entry = totals.setdefault((item['resourceType'], item['resourceId']), [0 if state['count'] is not None else None, 0])
                entry[1] += item['resourceQuantity']
                if entry[0] is not None and row['requirement'] <= state['count']:
                    entry[0] += item['resourceQuantity']
    with VSplit().set_w(1040).set_padding(18).set_sep(16).set_bg(roundrect_bg()).set_item_align('l'):
        with HSplit().set_sep(24).set_item_align('c'):
            TextBox('累计奖励', _birthday_style(21, True))
            if rows and state['count'] is not None:
                unlocked = sum(row['requirement'] <= state['count'] for row in rows)
                TextBox(f'已解锁 {unlocked}/{len(rows)} 档', _birthday_style(17))
        if rewards['preview']:
            TextBox('*奖励与称号按日服对应批次预览，以国服实装为准', _birthday_style(17, color=RED))
        if not rows:
            TextBox('目标内暂无累计奖励', _birthday_style(18))
        elif state['count'] is None:
            TextBox('*未获取到累计进度，无法确定下一档奖励', _birthday_style(17, color=RED))
        else:
            next_row = next((row for row in rows if row['requirement'] > state['count']), None)
            if next_row:
                await _draw_birthday_next_reward(data, next_row)
            else:
                TextBox('本目标内的累计奖励已全部解锁', _birthday_style(18, color=(50, 150, 50)))
        entries = list(totals.items())
        for start in range(0, len(entries), 4):
            with HSplit().set_sep(16).set_item_align('t'):
                for (kind, resource_id), (current, total) in entries[start:start + 4]:
                    name, picture = await _birthday_resource(data, kind, resource_id)
                    with Frame().set_w(239).set_content_align('lt'):
                        with HSplit().set_sep(8).set_item_align('c'):
                            # 名称统一预留两行，图标与文字总高固定为65px，避免随名称换行而缩放。
                            ImageBox(picture, size=(65, 65))
                            with VSplit().set_w(160).set_sep(3).set_item_align('l'):
                                name_size = 14 if get_text_size(get_font(DEFAULT_FONT, 15), name)[0] > 156 else 15
                                TextBox(name, _birthday_style(name_size), use_real_line_count=True).set_w(160).set_h(32).set_content_align('l')
                                TextBox(f'{current:,}/{total:,}' if current is not None else f'未知/{total:,}', _birthday_style(20, True, (50, 150, 50) if current == total else (25, 25, 25)))
                                _draw_birthday_reward_track(current, total, data['color'])
        if not honors:
            _draw_birthday_footer(data, honors)
    if not honors:
        return
    with VSplit().set_w(1040).set_padding(18).set_sep(16).set_bg(roundrect_bg()).set_item_align('l'):
        TextBox('生日称号', _birthday_style(21, True))
        for start in range(0, len(honors), 3):
            with HSplit().set_sep(16).set_item_align('t'):
                for flowers, item in honors[start:start + 3]:
                    picture = await _birthday_honor(data, item)
                    unlocked = state['count'] is not None and state['count'] >= flowers
                    label = '进度未知' if state['count'] is None else '已解锁' if unlocked else f"还差 {flowers - state['count']} 朵"
                    with VSplit().set_w(324).set_padding((12, 0)).set_sep(6).set_item_align('l'):
                        if picture is not None:
                            ImageBox(picture, size=(300, None))
                        else:
                            TextBox('称号素材暂缺', _birthday_style(20)).set_w(300).set_h(64).set_content_align('c')
                        with HSplit().set_w(300).set_h(34).set_sep(8).set_item_align('c'):
                            with Frame().set_w(142).set_h(34).set_content_align('c'):
                                with HSplit().set_sep(5).set_item_align('c'):
                                    icon = ImageBox(data['flower'], size=(28, 28))
                                    TextBox(f'{flowers} 朵', _birthday_style(20))
                            with Frame().set_w(150).set_h(34).set_content_align('c'):
                                status = TextBox(label, _birthday_style(20, color=(50, 150, 50) if unlocked else (125, 125, 125) if state['count'] is None else (25, 25, 25)))
                                status.set_padding((8, 4)).set_bg(RoundRectBg((50, 150, 50, 50) if unlocked else (80, 105, 130, 65), 5))
                            icon.set_size((status._get_self_size()[1], status._get_self_size()[1]))
        _draw_birthday_footer(data, honors)


# ================================ 页面准备与命令入口 ================================ #

async def compose_birthday_watering_image(ctx: SekaiHandlerContext, cid=None, target=None):
    """通过原账号、MasterData 和资源层准备数据，绘制完整生日浇水页面。"""
    uid = await get_player_bind_id(ctx, check_bind=False)
    now = int(datetime.now().timestamp() * 1000)
    parties, unit_rows, all_cards = await _birthday_gather(
        _birthday_table(ctx, 'birthdayParties'),
        ctx.md.game_character_units.get(),
        ctx.md.cards.get(),
    )
    parties = parties or []
    # 活动关联单元ID，卡牌与昵称使用角色ID；虚拟歌手的两种编号并不相同。
    units = {row['id']: row for row in unit_rows}
    party = _select_birthday_party(parties, cid, now, {key: row['gameCharacterId'] for key, row in units.items()})
    cid = units[party['gameCharacterUnitId']]['gameCharacterId'] if party else cid or min(range(1, 27), key=lambda value: get_character_next_birthday_dt(ctx.region, value))
    rules_ctx, reference, reference_schedule = ctx, False, None
    if party is None:
        matched = await _birthday_reference_party(ctx, parties, {key: row['gameCharacterId'] for key, row in units.items()}, cid, now)
        if matched is not None:
            rules_ctx, party, reference_schedule = matched
            reference = True
    cards = [row for row in all_cards if row['characterId'] == cid and row['cardRarityType'] == 'rarity_birthday']
    assert_and_reply(cards, '暂无该角色的生日卡数据')
    background_card = random.choice(cards)
    unit = units[party['gameCharacterUnitId']] if party and not reference else next(row for row in units.values() if row['gameCharacterId'] == cid)
    data = dict(ctx=ctx, uid=uid, cid=cid, party=party, now=now, color=color_code_to_rgb(unit['colorCode']),
                suite=None, capture=None, basic=None, avatar=None, images={}, resources={}, metadata={},
                reference=reference, reference_schedule=reference_schedule)
    if party:
        prepared, background = await _birthday_gather(
            _birthday_prepare(ctx, uid, party, rules_ctx),
            get_card_image(ctx, background_card, False, decode_in_pool=True),
        )
        suite, capture, basic, avatar, rewards, bonus_rows, delivery_rows = prepared
        if reference:
            rewards = {**rewards, 'preview': True, 'peer': rules_ctx}
        bonuses = [row for row in bonus_rows or [] if row['birthdayPartyId'] == party['id']]
        thresholds = {row['requirement'] for row in delivery_rows or [] if row['birthdayPartyId'] == party['id']}
        threshold = next(iter(thresholds)) if len(thresholds) == 1 and next(iter(thresholds)) > 0 else None
        maximum = max((row['requirement'] for row in rewards['rows']), default=None)
        assert_and_reply(target is None or maximum is None or target <= maximum, f'本期最高奖励目标为 {maximum} 朵')
        target = target if target is not None else maximum
        prestart = reference or now < party['startAt']
        # 日服活动ID仅用于取规则，不能拿它查国服抓包中的同号进度。
        state = _birthday_calculate(party, bonuses, threshold, target, suite, None if reference else capture, prestart, not suite and not capture)
        data.update(suite=suite, capture=capture, basic=basic, avatar=avatar, rewards=rewards, state=state, prestart=prestart)
        # 整期参考使用日服规则素材，避免逐次请求尚未实装的国服资源。
        material_ctx = rules_ctx if reference else ctx
        await _birthday_prepare_tables(data, material_ctx)
        material = next((row for row in await _birthday_table(material_ctx, 'mysekaiMaterials') or [] if row['id'] == party['deliveryRewardMysekaiMaterialId']), None)
        if material is None and ctx.region == 'cn' and not reference:
            material = next((row for row in await _birthday_table(SekaiHandlerContext.from_region('jp'), 'mysekaiMaterials') or [] if row['id'] == party['deliveryRewardMysekaiMaterialId']), None)
        data['flower'] = await _birthday_icon(data, material_ctx, f"mysekai/thumbnail/material/{material['iconAssetbundleName']}.png") if material else UNKNOWN_IMG
        dew = await _birthday_icon(data, material_ctx, f"thumbnail/material_rip/material{party['deliveryItemMaterialId']}.png")
    else:
        background = await get_card_image(ctx, background_card, False, decode_in_pool=True)
    with Canvas(bg=ImageBg(background)).set_padding(20) as canvas:
        with VSplit().set_w(1072).set_padding(16).set_sep(16).set_bg(roundrect_bg()).set_item_align('l'):
            with HSplit().set_sep(16).set_item_align('lt'):
                if party:
                    await _draw_birthday_profile(data)
                else:
                    with VSplit().set_w(512).set_padding(16).set_bg(roundrect_bg()):
                        TextBox(f'{get_region_name(ctx.region)}生日浇水', _birthday_style(26, True))
                await _draw_birthday_header(data)
            if party:
                # 背景由同行容器按最高面板统一绘制，缺数据时左右仍保持等高。
                with HSplit().set_sep(16).set_item_align('lt').set_item_bg(roundrect_bg()):
                    _draw_birthday_progress_panel(data, data['flower'], dew, True)
                    _draw_birthday_progress_panel(data, data['flower'], dew, False)
                _draw_birthday_requirement(data, dew)
                await _draw_birthday_cards(data, cards)
                await _draw_birthday_rewards(data)
            else:
                with VSplit().set_w(1040).set_padding(18).set_bg(roundrect_bg()):
                    TextBox(f'*暂无{get_region_name(ctx.region)}下一期生日浇水数据，暂无法计算', _birthday_style(20, color=RED))
                with VSplit().set_w(1040).set_padding(18).set_sep(14).set_bg(roundrect_bg()):
                    TextBox('生日卡', _birthday_style(21, True))
                    pictures = await batch_gather(*[get_card_full_thumbnail(ctx, card, False) for card in cards])
                    for start in range(0, len(cards), 6):
                        with HSplit().set_sep(16).set_item_align('c'):
                            for card, picture in zip(cards[start:start + 6], pictures[start:start + 6]):
                                with VSplit().set_sep(4).set_item_align('c'):
                                    ImageBox(picture, size=(128, 128))
                                    TextBox(str(card['id']), _birthday_style(16))
    add_watermark(canvas)
    return await canvas.get_img()


birthday_watering = SekaiCmdHandler(['/生日浇水'], regions=['cn', 'jp'])
birthday_watering.check_cdrate(cd).check_wblist(gbl)
@birthday_watering.handle()
async def _(ctx: SekaiHandlerContext):
    cid, target = _parse_birthday_args(ctx.get_args().strip())
    await ctx.block_region('birthday_watering')
    image = await compose_birthday_watering_image(ctx, cid, target)
    return await ctx.asend_reply_msg(await get_image_cq(image, low_quality=True))
