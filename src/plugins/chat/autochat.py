from ..record.sql import query_msg_by_id, query_recent_msg
from ..record import before_record_hook
from ..utils import *
from ..utils.rpc import *
from ..llm import ChatSession, ChatSessionResponse, get_text_embedding, download_image_to_b64

config = Config('chat.autochat')
logger = get_logger("Chat")
file_db = get_file_db("data/chat/db.json", logger)

JSON_RESTRAINT_TYPE_MAP = {
    'str': str,
    'int': int,
    'float': float,
    'bool': bool,
    'list': list,
    'dict': dict,
    'tuple': tuple,
    'NoneType': type(None),
}

chat_gwl = get_group_white_list(file_db, logger, 'chat')
autochat_gwl = get_group_white_list(file_db, logger, 'autochat', is_service=False)


# ------------------------------ 新聊天 ------------------------------ #

# 为每个客户端分别存储的新消息
message_pool: dict[str, list[dict]] = {}

# 记录新消息
@before_record_hook
async def record_new_message(bot: Bot, event: MessageEvent):
    global message_pool
    if not is_group_msg(event): return
    if not chat_gwl.check_id(event.group_id): return
    if not autochat_gwl.check_id(event.group_id): return
    user_name = get_user_name_by_event(event)
    msg = {
        'msg_id': event.message_id,
        'time': event.time,
        'user_id': event.user_id,
        'group_id': event.group_id,
        'nickname': user_name,
        'msg': get_msg(event),
    }
    for cid in message_pool:
        message_pool[cid].append(msg)


# ------------------------------ RPC服务 ------------------------------ #

RPC_SERVICE = 'autochat'

def on_connect(session: RpcSession):
    message_pool[session.id] = []

def on_disconnect(session: RpcSession):
    if session.id in message_pool:
        del message_pool[session.id]

start_rpc_service(
    host=config.get('rpc.host'),
    port=config.get('rpc.port'),
    token=config.get('rpc.token'),
    name=RPC_SERVICE,
    logger=logger,
    on_connect=on_connect,
    on_disconnect=on_disconnect
)


# 获取自身信息
@rpc_method(RPC_SERVICE, 'get_self_info')
async def handle_get_self_info(cid: str, group_id: int):
    bot = await aget_group_bot(group_id, raise_exc=True)
    return {
        'self_id': int(bot.self_id),
        'nickname': await get_group_member_name(group_id, int(bot.self_id)),
    }

# 获取所有开启的群组列表
@rpc_method(RPC_SERVICE, 'get_group_list')
async def handle_get_group_list(cid: str):
    group_ids = set(chat_gwl.get()).intersection(autochat_gwl.get())
    return [g for g in await get_all_bot_group_list() if int(g['group_id']) in group_ids]

# 发送群消息
@rpc_method(RPC_SERVICE, 'send_group_msg')
async def handle_send_group_msg(cid: str, group_id: int, message: list[dict] | str):
    if not chat_gwl.check_id(group_id) or not autochat_gwl.check_id(group_id):
        logger.warning(f"自动聊天取消发送消息到未启用群组 {group_id}")
        return
    bot = await aget_group_bot(group_id, raise_exc=True)
    if isinstance(message, str):
        message=Message(message)
    logger.info(f"自动聊天RPC客户端 {cid} 发送消息到群 {group_id}: {message}")
    return await bot.send_group_msg(group_id=int(group_id), message=message)

# 从数据库获取指定群历史聊天记录
@rpc_method(RPC_SERVICE, 'get_group_history_msg')
async def handle_get_group_msg(cid: str, group_id: int, limit: int):
    msgs = await query_recent_msg(group_id, limit)
    ret = []
    for msg in msgs:
        if check_is_bot_reply_msg(msg['msg_id']):
            continue
        if isinstance(msg['time'], datetime):
           msg['time'] = int(msg['time'].timestamp())
        ret.append(msg)
    return ret


@rpc_method(RPC_SERVICE, 'get_group_msg_by_id')
async def handle_get_group_msg_by_id(cid: str, group_id: int, msg_id: int):
    """为 Autochat 定向识图返回被引用的原始消息。"""

    msg = await query_msg_by_id(group_id, msg_id)
    if not msg:
        return None
    if isinstance(msg['time'], datetime):
        msg['time'] = int(msg['time'].timestamp())
    return msg


MAX_REFRESH_IMAGE_BYTES = 15 * 1024 * 1024


async def _image_ref_to_data_url(bot: Bot, file_ref: str) -> str:
    """Download a OneBot image reference and normalize it for the LLM."""

    file_ref = str(file_ref)
    if file_ref.startswith('http'):
        data_url = await download_image_to_b64(file_ref)
    else:
        image_path = str(await download_bot_file(bot, 'image', file_ref))
        if image_path.startswith('http'):
            data_url = await download_image_to_b64(image_path)
        else:
            if image_path.startswith('file://'):
                image_path = image_path[7:]
            if not os.path.isfile(image_path):
                raise FileNotFoundError(image_path)
            if os.path.getsize(image_path) > MAX_REFRESH_IMAGE_BYTES:
                raise ValueError("图片过大，拒绝刷新")
            image = open_image(image_path)
            try:
                data_url = get_image_b64(image)
            finally:
                image.close()
    if len(data_url) > MAX_REFRESH_IMAGE_BYTES * 2:
        raise ValueError("图片编码后过大，拒绝刷新")
    return data_url


@rpc_method(RPC_SERVICE, 'get_group_msg_image')
async def handle_get_group_msg_image(
    cid: str,
    group_id: int,
    msg_id: int,
    file_unique: str = "",
    file_ref: str = "",
):
    """Refresh one historical image through OneBot and return safe data URL bytes."""

    bot = await aget_group_bot(group_id, raise_exc=True)
    requested_unique = str(file_unique or "")
    requested_file = str(file_ref or "")
    try:
        msg_obj = await get_msg_obj_by_bot(bot, msg_id)
    except Exception as exc:
        logger.warning(f"获取历史消息 {msg_id} 失败，尝试直接取图: {get_exc_desc(exc)}")
        msg_obj = None

    # ================================ 精确消息刷新 ================================ #

    if msg_obj is None:
        direct_refs = []
        for candidate in (requested_file, requested_unique):
            if candidate and candidate not in direct_refs:
                direct_refs.append(candidate)
        for direct_ref in direct_refs:
            try:
                data_url = await _image_ref_to_data_url(bot, direct_ref)
                return {
                    'msg_id': int(msg_id),
                    'file_unique': requested_unique,
                    'data': data_url,
                }
            except Exception as exc:
                logger.warning(
                    f"直接刷新群 {group_id} 消息 {msg_id} 的图片失败: "
                    f"{get_exc_desc(exc)}"
                )
        return None

    # ================================ 消息内图片选择 ================================ #

    image_segments = [
        segment
        for segment in msg_obj.get('message', [])
        if segment.get('type') == 'image'
    ]
    matching_segments = [
        segment
        for segment in image_segments
        if (
            requested_unique
            and str(segment.get('data', {}).get('file_unique') or "") == requested_unique
        ) or (
            requested_file
            and str(segment.get('data', {}).get('file') or "") == requested_file
        )
    ]
    if matching_segments:
        image_segments = matching_segments
    elif len(image_segments) > 1 and (requested_unique or requested_file):
        # Do not silently summarize the wrong image when a multi-image message
        # has an unstable backend-specific identifier.
        return None

    for segment in image_segments:
        data = segment.get('data', {})
        current_unique = str(data.get('file_unique') or "")

        file_ref = data.get('file') or data.get('url')
        if not file_ref:
            continue
        try:
            data_url = await _image_ref_to_data_url(bot, file_ref)
            return {
                'msg_id': int(msg_obj.get('message_id', msg_id)),
                'file_unique': current_unique or requested_unique,
                'data': data_url,
            }
        except Exception as exc:
            logger.warning(
                f"刷新群 {group_id} 消息 {msg_id} 的图片失败: {get_exc_desc(exc)}"
            )
            continue
    return None

# 请求LLM
@rpc_method(RPC_SERVICE, 'query_llm')
async def handle_query_llm(cid: str, model: str | list[str], text: str, images: list[str], options: dict):
    timeout: int = options.get('timeout', 300)
    max_tokens: int = options.get('max_tokens', 2048)
    json_reply: bool = options.get('json_reply', False)
    json_key_restraints: list[dict] = options.get('json_key_restraints', [])

    imgs = []
    for img in images:
        if img.startswith('http'):
            try:
                img = await download_image_to_b64(img)
            except HttpError as exc:
                raise Exception(f"图片下载失败 HTTP {exc.status_code}") from exc
        imgs.append(img)

    session = ChatSession()
    session.append_user_content(text, imgs, verbose=False)

    def process(resp: ChatSessionResponse) -> str | dict:
        text = resp.result
        if not json_reply:
            return text
        
        try: 
            start_idx = text.find('{')
            end_idx = text.rfind('}')
            text = text[start_idx:end_idx+1]
            data = loads_json(text)
        except:
            raise Exception("解析回复为json失败")
        for restraint in json_key_restraints:
            key = restraint['key']
            dtypes = restraint.get('type')
            if isinstance(dtypes, str):
                dtypes = [dtypes]
            min_length = restraint.get('min_length')
            max_length = restraint.get('max_length')
            key = key.split('.')
            value = data
            for k in key:
                if k not in value:
                    raise Exception(f"回复的json缺少字段: {restraint['key']}")
                value = value[k]
            if dtypes:
                expected_types = []
                for dt in dtypes:
                    if dt not in JSON_RESTRAINT_TYPE_MAP:
                        raise Exception(f"字段 {restraint['key']} 包含不支持的类型约束: {dt}")
                    expected_types.append(JSON_RESTRAINT_TYPE_MAP[dt])
                if not isinstance(value, tuple(expected_types)):
                    raise Exception(f"字段 {restraint['key']} 类型错误，期望类型: {dtypes}")
            if isinstance(value, (str, list)):
                if min_length and len(value) < min_length:
                    raise Exception(f"字段 {restraint['key']} 长度过短，最小长度: {min_length}")
                if max_length and len(value) > max_length:
                    raise Exception(f"字段 {restraint['key']} 长度过长，最大长度: {max_length}")
        return data

    logger.info(f"自动聊天RPC客户端 {cid} 请求LLM模型")
    return await session.get_response(
        model_name=model,
        process_func=process,
        timeout=timeout,
        max_tokens=max_tokens,
    )

# 请求获取文本嵌入
@rpc_method(RPC_SERVICE, 'query_embedding')
async def handle_query_embedding(cid: str, texts: list[str], model_name: str):
    logger.info(f"自动聊天RPC客户端 {cid} 请求 {len(texts)} 条文本嵌入")
    embeddings = await get_text_embedding(texts, model_name)
    return embeddings

# 获取新消息，获取后清空
@rpc_method(RPC_SERVICE, 'get_new_msgs')
async def handle_get_new_msgs(cid: str):
    if cid not in message_pool:
        return []
    msgs = message_pool.get(cid, [])
    message_pool[cid] = []
    return msgs

