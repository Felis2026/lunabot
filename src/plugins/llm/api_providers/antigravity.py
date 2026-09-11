from openai import AsyncOpenAI

from ...llm.api_provider import ApiProvider


class AntigravityApiProvider(ApiProvider):
    """连接配置中指定的 OpenAI 兼容接口。"""

    def __init__(self):
        super().__init__(name="antigravity", code="ag")

    def get_client(self) -> AsyncOpenAI:
        # 接口地址由配置决定，可按容器、本机或远程部署方式分别设置。
        return AsyncOpenAI(
            api_key=self.get_api_key(),
            base_url=self.get_base_url(),
        )

    async def sync_quota(self):
        # 兼容接口未提供当前额度同步协议，跳过定时额度查询。
        return None
