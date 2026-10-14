"""原子工具层（Tool）。

- :class:`congclaw.tools.base.Tool` 定义原子工具基类（元信息 + 参数 schema + async run）；
- :mod:`congclaw.tools.catalog` 为内置六个业务工具；
- :mod:`congclaw.tools.registry` 提供动态注册与目录热插拔。

分工：**原子工具**进 LLM 的 ``bind_tools`` 工具表，由模型自主选择调用；
**编排技能**（:mod:`congclaw.skills`）由主图意图路由确定性挑选，二者不混用。
"""

from __future__ import annotations

from congclaw.tools.base import (
    ConfirmationRequired,
    Tool,
    ToolContext,
    ToolError,
    ToolParameter,
)

__all__ = [
    "ConfirmationRequired",
    "Tool",
    "ToolContext",
    "ToolError",
    "ToolParameter",
]
