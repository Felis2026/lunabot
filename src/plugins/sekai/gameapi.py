import os

from ..utils import *
from .common import *
from .handler import *

gameapi_config = Config('sekai.gameapi')

@dataclass
class GameApiConfig:
    api_status_url: Optional[str] = None
    profile_api_url: Optional[str] = None 
    suite_api_url: Optional[str] = None
    mysekai_api_url: Optional[str] = None  
    mysekai_photo_api_url: Optional[str] = None 
    mysekai_upload_time_api_url: Optional[str] = None 
    update_msr_sub_api_url: Optional[str] = None
    ranking_api_url: Optional[str] = None
    send_boost_api_url: Optional[str] = None
    create_account_api_url: Optional[str] = None
    ad_result_update_time_api_url: Optional[str] = None
    ad_result_api_url: Optional[str] = None


# 获取游戏api相关配置
def get_gameapi_config(ctx: SekaiHandlerContext) -> GameApiConfig:
    return GameApiConfig(**(gameapi_config.get(ctx.region, {})))


# 请求游戏API data_type: json/bytes/None
async def request_gameapi(url: str, method: str = 'GET', data_type: str | None = 'json', **kwargs):
    token = config.get('gameapi_token', '')
    # Default to zstd; can be overridden by gameapi_accept_encoding in sekai.yaml
    accept_encoding = config.get('gameapi_accept_encoding', 'zstd', raise_exc=False)
    extra_header_name = os.getenv('SEKAI_EXTRA_HEADER_NAME', '').strip()

    # Always send Authorization; optionally append one extra header from .env
    headers = {
        'Authorization': f'Bearer {token}',
        'Accept-Encoding': accept_encoding,
    }
    if extra_header_name:
        headers[extra_header_name] = token
    
    if 'headers' in kwargs:
        headers.update(kwargs['headers'])
        del kwargs['headers']

    try:
        async with get_client_session().request(method, url, headers=headers, verify_ssl=False, **kwargs) as resp:
            if resp.status != 200:
                try:
                    detail = await resp.text()
                    detail = loads_json(detail)['detail']
                except:
                    pass
                utils_logger.error(f"请求游戏API后端 {url} 失败: {resp.status} {detail}")
                raise HttpError(resp.status, detail)
            
            # 记录服务器实际返回的压缩格式（aiohttp会自动解压）
            content_encoding = (resp.headers.get('Content-Encoding') or '').lower()
            if content_encoding == 'zstd':
                utils_logger.info(f" aiohttp 已自动解压了 ZSTD 数据！接口: {url}")
            elif content_encoding:
                utils_logger.info(f" aiohttp 已自动解压了 {content_encoding.upper()} 数据！接口: {url}")
            else:
                utils_logger.info(f" 游戏API未返回压缩编码(Identity) 接口: {url}")
                
            if data_type is None:
                return resp
            
            # 下面的老逻辑完全不用动，因为 aiohttp 已经在 await resp.read/json() 的时候把解压工作做完了！
            elif data_type == 'json':
                if "text/plain" in resp.content_type:
                    return loads_json(await resp.text())
                elif "application/octet-stream" in resp.content_type:
                    import io
                    return loads_json(io.BytesIO(await resp.read()).read())
                else:
                    return await resp.json()
            elif data_type == 'bytes':
                return await resp.read()
            else:
                raise Exception(f"不支持的数据类型: {data_type}")
                
    except aiohttp.ClientConnectionError as e:
        raise Exception(f"连接游戏API后端失败，请稍后再试")
