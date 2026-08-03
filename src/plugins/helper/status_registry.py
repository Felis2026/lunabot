from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from ..utils.control_plane import get_group_service_matrix


# ================================ 状态结果模型 ================================ #

@dataclass
class StatusItem:
    """帮助页里的单个子状态项，例如“自动✅”或“日报❌”"""
    label: str
    enabled: bool


@dataclass
class HelpStatusResult:
    """
    /help 单个帮助页的状态结果。
    main_enabled 表示主功能开关；items 用于 chat / rollpig 这种一页多个状态的场景。
    """
    main_enabled: Optional[bool] = None
    items: list[StatusItem] = field(default_factory=list)

    def render(self) -> str:
        """
        渲染成可直接拼接到“服务名 - 描述”末尾的状态后缀。
        主开关关闭时只显示 ❌，避免子状态误导用户。
        """
        if self.main_enabled is None and not self.items:
            return ""

        if self.main_enabled is False:
            return " ❌"

        if self.items:
            text = " ".join(
                f"{item.label}{'✅' if item.enabled else '❌'}"
                for item in self.items
            )
            return f" [{text}]"

        if self.main_enabled is True:
            return " ✅"

        return ""


# ================================ 群状态上下文 ================================ #

@dataclass
class HelpStatusContext:
    """
    /help 一次请求只构造一次群状态快照，避免每个帮助页都重复查控制平面。
    """
    group_id: int
    toggle_map: dict[str, bool]

    @classmethod
    def from_group(cls, group_id: int) -> "HelpStatusContext":
        """从控制平面构造当前群的服务开关视图。"""
        matrix = get_group_service_matrix(int(group_id), include_non_service=True)
        return cls(
            group_id=int(group_id),
            toggle_map={
                str(item["name"]): bool(item["enabled"])
                for item in matrix
            },
        )

    def get_toggle(self, name: str) -> Optional[bool]:
        """按注册名读取某个群开关的当前状态。"""
        return self.toggle_map.get(name)


Resolver = Callable[[HelpStatusContext], Optional[HelpStatusResult]]


# ================================ 通用解析器工厂 ================================ #

def single_toggle(name: str) -> Resolver:
    """为“帮助页名 == 服务开关名”的场景生成标准解析器。"""
    def _resolve(ctx: HelpStatusContext) -> Optional[HelpStatusResult]:
        enabled = ctx.get_toggle(name)
        if enabled is None:
            return None
        return HelpStatusResult(main_enabled=enabled)

    return _resolve


# ================================ 组合帮助页解析 ================================ #

def resolve_chat(ctx: HelpStatusContext) -> Optional[HelpStatusResult]:
    """
    chat 帮助页同时覆盖主聊天、自动聊天和 @ 触发三个开关。
    主开关关闭时统一显示 ❌；主开关开启时只显示子状态块，保持列表简洁。
    """
    main_enabled = ctx.get_toggle("chat")
    if main_enabled is None:
        return None
    if not main_enabled:
        return HelpStatusResult(main_enabled=False)

    items: list[StatusItem] = []
    autochat_enabled = ctx.get_toggle("autochat")
    atchat_enabled = ctx.get_toggle("atchat")
    if autochat_enabled is not None:
        items.append(StatusItem("自动", autochat_enabled))
    if atchat_enabled is not None:
        items.append(StatusItem("@触发", atchat_enabled))

    return HelpStatusResult(main_enabled=True, items=items)


def resolve_msxray(ctx: HelpStatusContext) -> Optional[HelpStatusResult]:
    """
    msxray 优先读取控制平面注册名，兼容私有插件尚未接入统一服务名时的兜底查询。
    这样 /help 不需要知道插件内部是文件存储还是桥接注册。
    """
    enabled = ctx.get_toggle("MySekai Xray")
    if enabled is not None:
        return HelpStatusResult(main_enabled=enabled)

    try:
        from src.private.mysekai_xray_v2 import is_group_enabled
    except Exception:
        return None

    return HelpStatusResult(main_enabled=bool(is_group_enabled(ctx.group_id)))


def resolve_rollpig(ctx: HelpStatusContext) -> Optional[HelpStatusResult]:
    """
    rollpig 由私有插件自身决定“未接宿主控制系统时如何解释状态”，
    helper 只负责按插件暴露的 runtime 接口读取并展示。
    """
    try:
        from src.private.nonebot_plugin_rollpig_plus.runtime import (
            is_daily_summary_enabled,
            is_group_rollpig_enabled,
        )
    except Exception:
        try:
            from src.private.nonebot_plugin_rollpig.runtime import (
                is_daily_summary_enabled,
                is_group_rollpig_enabled,
            )
        except Exception:
            return None

    main_enabled = bool(is_group_rollpig_enabled(str(ctx.group_id)))
    return HelpStatusResult(main_enabled=main_enabled)


# ================================ 帮助页状态注册表 ================================ #

# 这里只维护“帮助页如何读状态”的规则，避免把映射逻辑散落到 helper 主流程里。
HELP_STATUS_REGISTRY: dict[str, Resolver] = {
    "bird": single_toggle("bird"),
    "broadcast": single_toggle("broadcast"),
    "chat": resolve_chat,
    "code": single_toggle("code"),
    "cron": single_toggle("cron"),
    "gallery": single_toggle("gallery"),
    "gallery-tag": single_toggle("gallery_tag"),
    "imgexp": single_toggle("imgexp"),
    "imgtool": single_toggle("imgtool"),
    "math": single_toggle("math"),
    "mc": single_toggle("mc"),
    "msxray": resolve_msxray,
    "nekochat": single_toggle("nekochat"),
    "random": single_toggle("random"),
    "record": single_toggle("record"),
    "rollpig": resolve_rollpig,
    "whateat": single_toggle("whateat"),
    "sekai": single_toggle("sekai"),
    "sta": single_toggle("sta"),
    "water": single_toggle("water"),
    "welcome": single_toggle("welcome"),
}


# ================================ 对 helper 暴露的接口 ================================ #

def build_help_status_context(group_id: Optional[int]) -> Optional[HelpStatusContext]:
    """群聊场景构造状态上下文，私聊或查询失败时返回 None。"""
    if not group_id:
        return None
    try:
        return HelpStatusContext.from_group(int(group_id))
    except Exception:
        return None


def get_help_status_result(help_name: str, ctx: Optional[HelpStatusContext]) -> Optional[HelpStatusResult]:
    """获取某个帮助页在当前群里的状态结果。"""
    if ctx is None:
        return None

    resolver = HELP_STATUS_REGISTRY.get(help_name)
    if resolver is None:
        return None

    try:
        result = resolver(ctx)
    except Exception:
        return None

    if not result:
        return None
    return result


def get_help_status_suffix(help_name: str, ctx: Optional[HelpStatusContext]) -> str:
    """获取某个帮助页在当前群里的状态后缀。"""
    result = get_help_status_result(help_name, ctx)
    if not result:
        return ""
    return result.render()


def get_help_status_prefix(help_name: str, ctx: Optional[HelpStatusContext]) -> str:
    """获取某个帮助页在当前群里的主状态前缀。"""
    result = get_help_status_result(help_name, ctx)
    if not result or result.main_enabled is None:
        return "❌"
    return "✅" if result.main_enabled else "❌"
