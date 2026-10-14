"""编排技能基类：用代码确定性地按业务顺序调用多个原子工具。

与原子工具（:mod:`congclaw.tools`）的分工：
- **技能**：编排逻辑写死在代码里，不进 LLM 的 ``bind_tools`` 工具表，只由主图
  ``intent_router`` 依据 :attr:`Skill.intent_hint` 挑选，控制流不依赖模型自由发挥；
- **工具**：单点业务动作，由模型自主选择调用。

对外参数仍是 :attr:`Skill.parameters`（由 intent_router 从用户话语中抽取的槽位）；
内部通过 :meth:`Skill.call_tool` / :meth:`Skill.try_call_tool` 调原子工具，每步自动发
``tool_call`` / ``tool_result`` 事件，前端与评测流水线无需改动。

约定 :meth:`Skill.run` 返回：``{"skill": name, "ok": bool, "steps": [...], "data": {...}}``；
``data`` 为最终答复所依据的关键业务数据（供答复节点消费）。

热插拔文件（catalog/ 与外置目录）只需在模块级暴露 ``SKILL`` 实例或 ``create_skill()``
工厂，注册中心即可动态加载（见 :mod:`congclaw.skills.registry`）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, ClassVar
from uuid import uuid4

from congclaw.tools.base import (
    ConfirmationRequired,
    Tool,
    ToolContext,
    ToolError,
    ToolParameter,
    parameters_metadata,
    validate_parameters,
)


@dataclass(frozen=True)
class SkillContext:
    """一次编排调用内的公共上下文（不显式暴露给 LLM 作为工具参数）。"""

    phone: str  # 当前会话的默认用户号码
    workspace: str = ""  # 会话工作区（pending approval 按会话隔离用）
    approval_mode: str = "inline"  # inline / auto / deny（复用 core.approval 语义）
    #: 编排技能取用原子工具用的注册中心（compare=False：不参与相等/哈希）
    tools: Any = None


class Skill:
    """所有编排技能的基类。子类设置类属性并实现 :meth:`run`。"""

    #: 技能唯一名（snake_case，与意图路由输出一致）
    name: ClassVar[str] = ""
    #: 技能一句话说明（管理接口展示用）
    description: ClassVar[str] = ""
    #: 给意图识别看的一句话适用条件（何时选择本编排）
    intent_hint: ClassVar[str] = ""
    #: read=只读；write=含写操作（触发反思校验，必要时人工确认）
    access: ClassVar[str] = "read"
    parameters: ClassVar[list[ToolParameter]] = []

    def __init__(self) -> None:
        if not self.name:
            raise ToolError(f"{type(self).__name__} 缺少 name 声明")
        if self.access not in ("read", "write"):
            raise ToolError(f"{self.name} 的 access 只能是 read/write")

    # ------------------------------------------------------------------
    # 元信息
    # ------------------------------------------------------------------

    def metadata(self) -> dict[str, Any]:
        """注册中心与管理接口使用的可 JSON 化元信息。"""
        return {
            "name": self.name,
            "description": self.description,
            "intent_hint": str(self.intent_hint or ""),
            "access": self.access,
            "parameters": parameters_metadata(self.parameters),
        }

    # ------------------------------------------------------------------
    # 参数校验
    # ------------------------------------------------------------------

    def validate_arguments(
        self, raw: dict[str, Any] | None, context: SkillContext | None = None
    ) -> dict[str, Any]:
        """按声明校验/强转入参（``context`` 仅为签名对齐，技能不使用其做校验）。"""
        del context
        return validate_parameters(self.name, self.parameters, raw)

    # ------------------------------------------------------------------
    # 结果装配（子类 run 返回值的统一形状）
    # ------------------------------------------------------------------

    @staticmethod
    def _step(name: str, ok: bool, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "name": name,
            "ok": bool(ok),
            "error": "" if ok else str((payload or {}).get("error") or ""),
        }

    def _finished(self, steps: list[dict[str, Any]], data: dict[str, Any]) -> dict[str, Any]:
        return {"skill": self.name, "ok": True, "steps": steps, "data": data}

    def _failed(
        self, steps: list[dict[str, Any]], error: str, error_code: str = "tool_error"
    ) -> dict[str, Any]:
        return {
            "skill": self.name,
            "ok": False,
            "steps": steps,
            "data": {},
            "error": error,
            "error_code": error_code,
        }

    # ------------------------------------------------------------------
    # 取用原子工具
    # ------------------------------------------------------------------

    def _registry_for(self, context: SkillContext) -> Any:
        registry = getattr(context, "tools", None)
        if registry is not None:
            return registry
        from congclaw.tools.registry import get_tool_registry

        return get_tool_registry()

    def _resolve(self, context: SkillContext, name: str) -> Tool:
        tool = self._registry_for(context).try_get(name)
        if tool is None:
            raise ToolError(
                f"编排技能 {self.name} 依赖的工具 {name} 未注册（可能已被热卸载）",
                code="unknown_tool",
            )
        return tool

    def _confirmation_summary(self, tool: Tool, context: ToolContext, args: dict[str, Any]) -> str:
        if hasattr(tool, "confirmation_summary"):
            return str(tool.confirmation_summary(context, args))  # type: ignore[attr-defined]
        return (
            f"即将为您办理「{tool.name}」，参数：{args}。"
            "请确认是否办理？（回复「确认」继续，回复「取消」放弃）"
        )

    async def call_tool(self, context: SkillContext, name: str, **args: Any) -> dict[str, Any]:
        """调用一个原子工具；失败抛 :class:`ToolError`（编排据此决定中断或继续）。

        命中「需人工确认的写操作」时抛 :class:`ConfirmationRequired`，本步不执行，
        且不发 ``tool_call``（与既有「确认卡片与执行互斥」的轨迹约束一致）。
        """
        tool = self._resolve(context, name)
        if tool.access == "write" and tool.requires_confirmation:
            mode = str(getattr(context, "approval_mode", "inline") or "inline")
            if mode == "deny":
                raise ToolError(
                    f"当前审批策略为 deny，写操作 {name} 未获授权，已拒绝执行。",
                    code="approval_denied",
                )
            if mode != "auto":
                raise ConfirmationRequired(
                    name,
                    dict(args),
                    self._confirmation_summary(tool, _tool_context(context), args),
                )

        call_id = f"sk-{self.name}-{uuid4().hex[:8]}"
        _emit_event(
            {
                "type": "tool_call",
                "name": name,
                "call_id": call_id,
                "args": args,
                "operator": self.name,
            }
        )
        try:
            data = await tool.run(_tool_context(context), **args)
        except ToolError as exc:
            _emit_event(
                {
                    "type": "tool_result",
                    "name": name,
                    "call_id": call_id,
                    "ok": False,
                    "error": str(exc),
                    "preview": "",
                    "operator": self.name,
                }
            )
            raise
        except Exception as exc:  # noqa: BLE001 —— 非业务异常同样记为工具失败，交反思决策
            _emit_event(
                {
                    "type": "tool_result",
                    "name": name,
                    "call_id": call_id,
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "preview": "",
                    "operator": self.name,
                }
            )
            raise ToolError(f"{type(exc).__name__}: {exc}", code="tool_exception") from exc
        _emit_event(
            {
                "type": "tool_result",
                "name": name,
                "call_id": call_id,
                "ok": True,
                "error": "",
                "preview": _preview(data),
                "operator": self.name,
            }
        )
        return data if isinstance(data, dict) else {"value": data}

    async def try_call_tool(
        self, context: SkillContext, name: str, **args: Any
    ) -> tuple[bool, dict[str, Any]]:
        """调用原子工具并把失败收敛为 ``(False, {"error": ...})``，便于编排继续推进。

        :class:`ConfirmationRequired` 属于控制流信号，不在此吞掉，继续向上抛给执行节点。
        """
        try:
            return True, await self.call_tool(context, name, **args)
        except ConfirmationRequired:
            raise
        except ToolError as exc:
            return False, {"error": str(exc), "error_code": exc.code}
        except Exception as exc:  # noqa: BLE001
            return False, {"error": f"{type(exc).__name__}: {exc}", "error_code": "tool_exception"}

    async def run(self, context: SkillContext, **kwargs: Any) -> dict[str, Any]:
        """编排入口，子类必须实现；返回值形状见模块文档。"""
        raise NotImplementedError


def _tool_context(context: SkillContext) -> ToolContext:
    """把编排上下文降维成原子工具上下文（工具不感知注册中心）。"""
    return ToolContext(
        phone=context.phone,
        workspace=context.workspace,
        approval_mode=context.approval_mode,
    )


def _emit_event(payload: dict[str, Any]) -> None:
    """把编排步骤事件写入当前图的 custom stream（图外调用时静默忽略）。"""
    try:
        from langgraph.config import get_stream_writer

        get_stream_writer()(payload)
    except Exception:  # noqa: BLE001 —— 事件写入失败绝不能影响业务执行
        return


def _preview(data: Any, limit: int = 240) -> str:
    if not isinstance(data, dict):
        return ""
    return json.dumps(data, ensure_ascii=False, default=str)[:limit]


__all__ = ["ConfirmationRequired", "Skill", "SkillContext"]
