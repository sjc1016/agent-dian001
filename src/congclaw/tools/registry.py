"""原子工具动态注册中心 + 目录热加载。

- 内置目录 ``tools/catalog/``（六个业务工具）+ 外置目录（默认 ``data/tools/``，
  可用环境变量 ``TOOLS_EXTRA_DIR`` 覆盖），扫描/监听/热插拔逻辑复用
  :class:`congclaw.core.hotload.HotLoadRegistry`；
- 热插拔文件约定：模块级暴露 ``TOOL``（:class:`~congclaw.tools.base.Tool` 实例）
  或 ``create_tool()`` 工厂。

原子工具是 LLM 唯一可直接调用的能力：思考节点把 :meth:`ToolRegistry.openai_tools`
交给 ``model.bind_tools``，模型自主选择调用；编排技能（:mod:`congclaw.skills`）
不进工具表，由意图路由确定性挑选。
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from congclaw.core.hotload import HotLoadRegistry, watch_enabled
from congclaw.tools.base import Tool

BUILTIN_CATALOG_DIR = Path(__file__).parent / "catalog"
DEFAULT_EXTRA_DIR = Path("data") / "tools"


class ToolRegistry(HotLoadRegistry):
    """扫描目录、动态加载并按名取用原子工具的注册中心。"""

    item_label = "工具"
    symbol_name = "TOOL"
    factory_name = "create_tool"
    item_type = Tool

    @classmethod
    def builtin_dir(cls) -> Path:
        return BUILTIN_CATALOG_DIR

    @classmethod
    def extra_dir_env(cls) -> str:
        return "TOOLS_EXTRA_DIR"

    @classmethod
    def default_extra_dir(cls) -> Path:
        return DEFAULT_EXTRA_DIR

    # ------------------------------------------------------------------
    # 供 LLM 的工具描述
    # ------------------------------------------------------------------

    def openai_tools(self) -> list[dict[str, Any]]:
        """思考节点 ``bind_tools`` 用的工具描述列表（注册即生效，天然支持热插拔）。"""
        return [tool.to_openai_tool() for tool in self.all()]


# ---------------------------------------------------------------------------
# 进程内单例（FastAPI lifespan 管理生命周期）
# ---------------------------------------------------------------------------

_registry: ToolRegistry | None = None
_registry_lock = threading.Lock()


def get_tool_registry() -> ToolRegistry:
    """获取进程内共享工具注册中心（首次创建时扫描目录，默认开启目录监听）。"""
    global _registry
    with _registry_lock:
        if _registry is None:
            _registry = ToolRegistry(watch=watch_enabled())
        return _registry


def shutdown_tool_registry() -> None:
    global _registry
    with _registry_lock:
        if _registry is not None:
            _registry.stop_watching()
        _registry = None


__all__ = [
    "BUILTIN_CATALOG_DIR",
    "DEFAULT_EXTRA_DIR",
    "ToolRegistry",
    "get_tool_registry",
    "shutdown_tool_registry",
]
