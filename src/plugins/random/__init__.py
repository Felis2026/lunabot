from ..utils import *

config = Config('random')
logger = get_logger("Random")
file_db = get_file_db("data/random/db.json", logger)
cd = ColdDown(file_db, logger)
gbl = get_group_black_list(file_db, logger, 'random', allow_group_admin_current_group=True)


# ================================ 参数约束与解析 ================================ #

# 群友抽取会直接影响单条消息中的图片数量，限制为 10 人可避免大群中
# 一次生成过多头像消息。选项类指令也限制总长度和数量，防止超长回复。
MAX_RANDOM_MEMBER_COUNT = 10
MAX_RANDOM_OPTION_COUNT = 100
MAX_RANDOM_ARGUMENT_LENGTH = 2000


def _parse_integer(value: str, error_message: str) -> int:
    """解析用户输入的整数，并把底层转换异常替换为可读提示。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ReplyException(error_message)


def _parse_roll_range(args: list[str]) -> tuple[int, int]:
    """解析随机数边界，保持原有三种合法调用方式。"""
    usage = '参数格式错误，请使用 /roll、/roll 上限 或 /roll 下限 上限'
    if not args:
        return 0, 100
    if len(args) == 1:
        upper = _parse_integer(args[0], usage)
        assert_and_reply(upper >= 1, '只填写一个参数时，上限必须大于等于 1')
        return 1, upper
    if len(args) == 2:
        lower = _parse_integer(args[0], usage)
        upper = _parse_integer(args[1], usage)
        assert_and_reply(lower <= upper, '左边界不能大于右边界')
        return lower, upper
    raise ReplyException(usage)


def _parse_options(ctx: HandlerContext, example: str) -> list[str]:
    """解析选择类指令参数，并限制可能产生的回复规模。"""
    raw_args = ctx.get_args().strip()
    assert_and_reply(
        len(raw_args) <= MAX_RANDOM_ARGUMENT_LENGTH,
        f'参数内容过长，最多支持 {MAX_RANDOM_ARGUMENT_LENGTH} 个字符',
    )
    choices = raw_args.split()
    assert_and_reply(len(choices) >= 2, f'至少需要两个选项，例如：{example}')
    assert_and_reply(
        len(choices) <= MAX_RANDOM_OPTION_COUNT,
        f'选项过多，最多支持 {MAX_RANDOM_OPTION_COUNT} 个',
    )
    return choices


def _parse_random_member_count(arg_text: str) -> int:
    """解析随机群友人数；空参数默认抽取一人。"""
    args = arg_text.strip().split()
    if not args:
        return 1
    assert_and_reply(len(args) == 1, '抽取人数只能填写一个整数')
    count = _parse_integer(args[0], '抽取人数必须是整数')
    assert_and_reply(
        1 <= count <= MAX_RANDOM_MEMBER_COUNT,
        f'抽取人数必须在 1～{MAX_RANDOM_MEMBER_COUNT} 之间',
    )
    return count


# ================================ 博饼素材加载 ================================ #

dice_images = None
dice_rule_image = None
try:
    DICE_SIZE = 32
    loaded_dice_images = []
    for dice_path in [f"data/random/dice/{i}.png" for i in range(1, 7)]:
        with Image.open(dice_path) as dice_image:
            loaded_dice_images.append(dice_image.resize((DICE_SIZE, DICE_SIZE)).copy())
    with Image.open("data/random/dice_rule.jpg") as rule_image:
        loaded_rule_image = rule_image.copy()
    dice_images = loaded_dice_images
    dice_rule_image = loaded_rule_image
except Exception as e:
    logger.warning(f"博饼素材加载失败，相关功能暂不可用: {get_exc_desc(e)}")


# 博饼
bing = CmdHandler(["/bing", "/bobing", "/博饼", "/饼"], logger)
bing.check_cdrate(cd).check_wblist(gbl)
@bing.handle()
async def _(ctx: HandlerContext):
    assert_and_reply(dice_images is not None, '博饼功能暂不可用')
    dices = [random.randint(1, 6) for _ in range(6)]
    image = Image.new('RGBA', (DICE_SIZE * 6, DICE_SIZE * 2), (255, 255, 255, 0))
    for i, dice in enumerate(dices):
        image.paste(dice_images[dice - 1], (i * DICE_SIZE, DICE_SIZE // 2))
    with TempFilePath('gif') as save_path:
        save_transparent_static_gif(image, save_path)
        await ctx.asend_reply_msg(await get_image_cq(save_path))


# 博饼规则
bing_rule = CmdHandler(["/bingrule", "/bing_rule", "/bing rule", 
                        "/bobing_rule", "/bobingrule", "/bobing rule",
                        "/博饼规则", "/博饼 规则", "/饼 规则", "/饼规则"], logger)
bing_rule.check_cdrate(cd).check_wblist(gbl)
@bing_rule.handle()
async def _(ctx: HandlerContext):
    assert_and_reply(dice_rule_image is not None, '博饼功能暂不可用')
    return await ctx.asend_reply_msg(await get_image_cq(dice_rule_image, low_quality=True))


# 随机数
rand = CmdHandler(["/rand", "/roll", "/随机数"], logger)
rand.check_cdrate(cd).check_wblist(gbl)
@rand.handle()
async def _(ctx: HandlerContext):
    args = ctx.get_args().strip().split()
    lower, upper = _parse_roll_range(args)
    msg = f'{random.randint(lower, upper)}'
    return await ctx.asend_reply_msg(msg)


# 随机选择
choice = CmdHandler(["/choice", '/choose', "/选择"], logger)
choice.check_cdrate(cd).check_wblist(gbl)
@choice.handle()
async def _(ctx: HandlerContext):
    choices = _parse_options(ctx, '/choose 苹果 香蕉')
    msg = f'选择: {random.choice(choices)}'
    return await ctx.asend_reply_msg(msg)


# 打乱
shuffle = CmdHandler(["/shuffle", "/洗牌", "/打乱"], logger)
shuffle.check_cdrate(cd).check_wblist(gbl)
@shuffle.handle()
async def _(ctx: HandlerContext):
    choices = _parse_options(ctx, '/shuffle 1 2 3 4')
    random.shuffle(choices)
    msg = f'{", ".join(choices)}'
    return await ctx.asend_reply_msg(msg)


# 随机群成员
randuser = CmdHandler(['/randuser', '/rolluser', '/randmember', '/rollmember', "/随机群友"], logger)
randuser.check_cdrate(cd).check_wblist(gbl)
@randuser.handle()
async def _(ctx: HandlerContext):
    assert_and_reply(ctx.group_id is not None, '随机群友仅支持在群聊中使用')
    num = _parse_random_member_count(ctx.get_args())

    group_members = await get_group_users(ctx.bot, ctx.group_id)
    bot_user_id = int(ctx.bot.self_id)
    available_members = []
    for user in group_members:
        try:
            user_id = int(user.get('user_id'))
        except (TypeError, ValueError):
            continue
        if user_id != bot_user_id:
            available_members.append(user)

    assert_and_reply(num <= len(available_members), f'当前可抽取群成员不足 {num} 人')

    # get_group_member_list 已包含群名片和昵称，无需再逐人请求成员详情。
    # 头像 URL 直接交给 NapCat 处理，避免 Bot 下载并重复转换临时图片。
    selected_members = random.sample(available_members, num)
    message_parts = []
    for user in selected_members:
        user_id = int(user['user_id'])
        icon_url = await get_avatar_url(ctx.bot, user_id)
        icon_cq = await get_image_cq(icon_url, send_url_as_is=True)
        nickname = user.get('card') or user.get('nickname') or str(user_id)
        nickname = str(MessageSegment.text(str(nickname)))
        message_parts.append(f"{icon_cq}\n{nickname}({user_id})")

    return await ctx.asend_reply_msg("\n".join(message_parts))
