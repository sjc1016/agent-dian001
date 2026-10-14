"""阶段 4：Agent 深度推理子图节点（全部 async，LangGraph 调度器并发执行）。

拓扑见 :func:`congclaw.agent.workflow.build_agent_subgraph`：

    START → approval_entry ─┬─ think ─→ act（asyncio.gather 多工具并行）
                            │              └→ reflect ─┬─ pass → finalize → END
                            │              ┌───────────┼─ retry → think（attempts+1）
                            ├─ act（确认恢复）──────────┴─ fallback → agent_fallback → END
                            └─ finalize（取消/无需工具/确认卡片）→ END

关键设计：
- 思考节点通过工具注册中心实时 ``bind_tools``（热插拔工具当轮即可被发现）；
- 写操作（change_package）在 inline 审批模式下先落 pending_approval 并返回确认卡片，
  用户下一轮回复"确认/取消"，由 approval_entry 恢复执行或作废（P4-16）；
- 工具异常/反思失败计入 attempts，超阈值走静态兜底话术（P4-15）。
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any
from uuid import uuid4

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.config import get_stream_writer

from congclaw.agent.state import AgentSubState
from congclaw.core.approval import ApprovalRequest, make_approval_request
from congclaw.domain.business_store import (
    DEFAULT_DEMO_PHONE,
    save_pending_approval,
    update_pending_approval,
)
from congclaw.graph.memory import compose_layered_content
from congclaw.prompts.agent import (
    AGENT_FALLBACK_REPLY,
    AGENT_REFLECT_ERROR_REPLY,
    AGENT_REFLECT_PROMPT,
    AGENT_RESPOND_ERROR_REPLY,
    AGENT_RESPOND_PROMPT,
    AGENT_THINK_ERROR_REPLY,
    AGENT_THINK_PROMPT,
    APPROVAL_CANCELLED_REPLY,
)
from congclaw.providers.openai_provider import create_model
from congclaw.skills.base import SkillContext
from congclaw.skills.registry import SkillRegistry, get_skill_registry
from congclaw.tools.base import ConfirmationRequired, ToolContext, ToolError
from congclaw.tools.registry import ToolRegistry, get_tool_registry

# ---------------------------------------------------------------------------
# P4-16 辅助：跨轮"确认/取消"意图识别（规则优先，确定可测，不额外消耗 LLM）
# ---------------------------------------------------------------------------

_CANCEL_KEYWORDS = ("取消", "算了", "不用", "不要", "不办", "放弃", "先不", "不必", "不需要", "no")
_CONFIRM_KEYWORDS = (
    "确认", "确定", "是的", "好的", "好吧", "可以", "没问题", "同意",
    "办理", "开通", "就行", "继续", "嗯嗯",
)


def detect_approval_resolution(text: str) -> str:
    """识别用户对人工确认卡片的回复：confirmed / cancelled / unknown。"""
    normalized = re.sub(r"\s+", "", str(text or "").strip().lower())
    if not normalized:
        return "unknown"
    if any(keyword in normalized for keyword in _CANCEL_KEYWORDS):
        return "cancelled"
    if normalized in {"y", "yes", "是", "嗯", "好", "行", "可", "对"}:
        return "confirmed"
    if any(keyword in normalized for keyword in _CONFIRM_KEYWORDS):
        return "confirmed"
    return "unknown"


# ---------------------------------------------------------------------------
# P4-16：跨轮审批恢复入口
# ---------------------------------------------------------------------------


async def approval_entry_node(state: AgentSubState) -> dict[str, Any]:
    """有上轮未决办理时，按用户本轮"确认/取消/其它"恢复或作废。"""
    writer = _get_writer()
    pending = state.get("pending_approval") or None
    resolution = str(state.get("approval_resolution") or "unknown")
    if not pending:
        # 主图 intent_router 选定编排技能时直接交编排执行（跳过 think）
        return {"entry_decision": "skill" if _selected_skill(state) else "think"}

    approval_id = str(pending.get("approval_id") or "")
    if resolution == "cancelled":
        if approval_id:
            await update_pending_approval(approval_id, "cancelled")
        writer(
            {
                "type": "approval_cancelled",
                "approval_id": approval_id,
                "tool_name": pending.get("tool_name", ""),
            }
        )
        return {
            "entry_decision": "cancel",
            "answer": APPROVAL_CANCELLED_REPLY,
            "pending_approval": None,
        }

    if resolution == "confirmed":
        call = {
            "id": f"call-{uuid4().hex[:8]}",
            "name": str(pending.get("tool_name") or ""),
            "args": dict(pending.get("args") or {}),
            "approved": True,
        }
        if approval_id:
            await update_pending_approval(approval_id, "approved")
        writer(
            {
                "type": "approval_resumed",
                "approval_id": approval_id,
                "tool_name": call["name"],
                "args": call["args"],
            }
        )
        return {
            "entry_decision": "act",
            "tool_calls": [call],
            "last_approved_call": call,
            "attempts": int(state.get("attempts", 0) or 0),
            # 注意：pending_approval 必须保留到 act_node，执行后才能回写 executed/failed
        }

    # 用户没有明确确认/取消而是说了别的内容：旧确认单作废，按新诉求重新处理
    if approval_id:
        await update_pending_approval(approval_id, "cancelled")
    writer({"type": "approval_expired", "approval_id": approval_id})
    return {
        "entry_decision": "skill" if _selected_skill(state) else "think",
        "pending_approval": None,
    }


def approval_entry_route(state: AgentSubState) -> str:
    return str(state.get("entry_decision") or "think")


def _selected_skill(state: AgentSubState) -> str:
    """主图 intent_router 选中的编排技能名（未选中返回空串）。"""
    return str(state.get("skill_name") or "").strip()


# ---------------------------------------------------------------------------
# P4-12：思考节点（planner 客服域改造）
# ---------------------------------------------------------------------------


async def think_node(state: AgentSubState) -> dict[str, Any]:
    """分析对话状态决定本轮工具调用；无需工具时直接给出追问/解释文本。"""
    writer = _get_writer()
    registry = _get_tools(state)
    attempts = int(state.get("attempts", 0) or 0)

    messages = _build_think_messages(state, registry)
    try:
        model = create_model()
        if registry.all():
            model = model.bind_tools(registry.openai_tools())
        response = await model.ainvoke(messages)
    except Exception as exc:  # LLM 异常：可重试一次，超限直接错误话术，避免死循环
        error = f"{type(exc).__name__}: {exc}"
        writer({"type": "agent_think_error", "error": error, "attempts": attempts + 1})
        if attempts + 1 >= int(state.get("max_attempts", 2) or 2):
            return {
                "direct_answer": AGENT_THINK_ERROR_REPLY,
                "fallback_reason": "think_error",
                "attempts": attempts + 1,
            }
        return {"attempts": attempts + 1, "tool_calls": [], "reasoning": ""}

    raw_calls = list(getattr(response, "tool_calls", None) or [])
    content = str(getattr(response, "content", "") or "").strip()

    valid_calls: list[dict[str, Any]] = []
    unknown_names: list[str] = []
    for raw in raw_calls:
        name = str(raw.get("name") or "")
        args = raw.get("args") or {}
        if not isinstance(args, dict):
            args = {}
        # 只有原子工具能进工具表；编排技能名不在此表内，一律按未注册处理
        if registry.try_get(name) is None:
            unknown_names.append(name)
            continue
        valid_calls.append(
            {"id": str(raw.get("id") or f"call-{uuid4().hex[:8]}"), "name": name, "args": args}
        )

    if unknown_names:
        writer({"type": "agent_unknown_tool", "names": unknown_names})

    if valid_calls:
        writer(
            {
                "type": "agent_thinking",
                "reasoning": content,
                "attempt": attempts + 1,
                "calls": [
                    {"name": call["name"], "args": call["args"], "call_id": call["id"]}
                    for call in valid_calls
                ],
            }
        )
        return {"tool_calls": valid_calls, "reasoning": content, "direct_answer": ""}

    # 无工具调用：模型直接回复（追问补齐信息 / 寒暄 / 纯解释）
    direct_answer = content or ""
    if not direct_answer:
        if attempts + 1 >= int(state.get("max_attempts", 2) or 2):
            direct_answer = AGENT_THINK_ERROR_REPLY
            return {
                "direct_answer": direct_answer,
                "fallback_reason": "empty_plan",
                "attempts": attempts + 1,
            }
        attempts += 1
    writer(
        {
            "type": "agent_thinking",
            "reasoning": direct_answer[:200],
            "attempt": attempts + 1,
            "calls": [],
        }
    )
    return {"tool_calls": [], "direct_answer": direct_answer, "attempts": attempts}


def think_route(state: AgentSubState) -> str:
    if state.get("tool_calls"):
        return "act"
    return "finalize"


def _build_think_messages(state: AgentSubState, registry: ToolRegistry) -> list[Any]:
    system_prompt = AGENT_THINK_PROMPT
    # 工具注册表只登记原子工具：编排技能不进 bind_tools，也不出现在候选表里
    catalog = registry.list_metadata()
    if catalog:
        lines = ["\n\n当前可用业务工具（热插拔注册表实时提供）："]
        for item in catalog:
            params = ", ".join(
                f"{p['name']}{'*' if p['required'] else ''}" for p in item.get("parameters", [])
            )
            lines.append(
                f"- {item['name']}（{'写' if item['access'] == 'write' else '只读'}"
                f"{'，需人工确认' if item.get('requires_confirmation') else ''}；参数：{params or '无'}）"
            )
        system_prompt += "\n".join(lines)

    extra_sections: list[str] = []
    phone = str(state.get("phone") or DEFAULT_DEMO_PHONE)
    extra_sections.append(f"【当前会话绑定号码】\n{phone}（用户未显式提供号码时查询/办理该号码）")
    hint = str(state.get("verify_hint") or "").strip()
    if hint:
        extra_sections.append(
            f"【上轮反思校验未通过，请据此修正】\n{hint}\n"
            "（可修正参数后重新调用工具，或补充调用其他工具核实）"
        )
    # 四层组装：记忆（短期窗口+长期摘要）→ 附加信息 → 用户诉求（P5-9）
    content = compose_layered_content(
        question=str(state.get("query", "")),
        memory_text=_agent_memory_text(state),
        extra_sections=extra_sections,
    )
    return [SystemMessage(content=system_prompt), HumanMessage(content=content)]


def _agent_memory_text(state: AgentSubState) -> str:
    """记忆层文本：优先主图预渲染的四层记忆，缺省时回退旧 session_context。"""
    memory_text = str(state.get("memory_context") or "").strip()
    if memory_text:
        return memory_text
    session_context = str(state.get("session_context") or "").strip()
    if session_context:
        return "【会话记忆（近期对话，仅供理解指代，勿泄露原始 JSON）】\n" + session_context
    return ""


# ---------------------------------------------------------------------------
# P4-13：工具编排节点（单轮多工具并行 asyncio.gather；P4-16 审批短路）
# ---------------------------------------------------------------------------


async def act_node(state: AgentSubState) -> dict[str, Any]:
    writer = _get_writer()
    registry = _get_tools(state)
    context = ToolContext(
        phone=str(state.get("phone") or DEFAULT_DEMO_PHONE),
        workspace=str(state.get("workspace") or ""),
        approval_mode=str(state.get("approval_mode") or "inline"),
    )
    calls = list(state.get("tool_calls") or [])

    executable: list[tuple[dict[str, Any], Any]] = []
    confirmation: dict[str, Any] | None = None
    results: list[dict[str, Any]] = []

    for call in calls:
        tool = registry.try_get(str(call.get("name") or ""))
        if tool is None:
            results.append(
                {
                    "call_id": call.get("id", ""),
                    "name": call.get("name", ""),
                    "ok": False,
                    "data": {},
                    "error": f"工具 {call.get('name')} 未注册（可能已被热卸载）",
                    "error_code": "unknown_tool",
                }
            )
            continue

        if tool.access == "write" and tool.requires_confirmation and not call.get("approved"):
            if context.approval_mode == "auto":
                call["approved"] = True
                executable.append((call, tool))
            elif context.approval_mode == "deny":
                results.append(
                    {
                        "call_id": call.get("id", ""),
                        "name": tool.name,
                        "ok": False,
                        "data": {},
                        "error": "当前审批策略为 deny，写操作未获授权，已拒绝执行。",
                        "error_code": "approval_denied",
                    }
                )
            else:
                # inline：落 pending approval + 返回确认卡片，本轮不执行该写操作
                confirmation = await _build_tool_confirmation(tool, context, call, writer)
        else:
            executable.append((call, tool))

    if executable:
        coros = [_execute_tool(call, tool, context, writer) for call, tool in executable]
        results.extend(await asyncio.gather(*coros))

    # 保持结果顺序与调用顺序一致（gather 已保序，加上前置 error 结果后统一按 calls 排序）
    order = {call.get("id", ""): index for index, call in enumerate(calls)}
    results.sort(key=lambda item: order.get(str(item.get("call_id")), 999))

    update: dict[str, Any] = {"tool_results": results}

    pending = state.get("pending_approval") or None
    approved_call = state.get("last_approved_call") or None
    if approved_call is not None and pending:
        approval_id = str(pending.get("approval_id") or "")
        approved_result = next(
            (
                item
                for item in results
                if item.get("name") == approved_call.get("name") and item.get("ok")
            ),
            None,
        )
        if approval_id:
            await update_pending_approval(
                approval_id, "executed" if approved_result is not None else "failed"
            )
        # 确认单本轮终结：清空未决状态，避免后续重试轮重复回写
        update["pending_approval"] = None

    if confirmation is not None:
        update["confirmation_request"] = confirmation
        writer(
            {
                "type": "agent_confirm_required",
                "approval_id": confirmation["approval_id"],
                "tool_name": confirmation["tool_name"],
                "summary": confirmation["summary"],
            }
        )
    return update


def act_route(state: AgentSubState) -> str:
    if state.get("confirmation_request"):
        return "finalize"
    return "reflect"


async def _build_tool_confirmation(
    tool: Any, context: ToolContext, call: dict[str, Any], writer
) -> dict[str, Any]:
    args = dict(call.get("args") or {})
    if hasattr(tool, "confirmation_summary"):
        summary = tool.confirmation_summary(context, args)
    else:
        summary = (
            f"即将为您办理「{tool.name}」，参数：{json.dumps(args, ensure_ascii=False)}。"
            "请确认是否办理？（回复「确认」继续，回复「取消」放弃）"
        )
    # 复用现有 ApprovalRequest 结构（tool_name 字段承载待确认的原子工具名）
    request = ApprovalRequest(
        id=make_approval_request(command=summary, risk_reason="").id,
        command=summary,
        risk_reason=f"高危业务写操作：{tool.name}",
        tool_name=tool.name,
    )
    await save_pending_approval(
        context.workspace,
        approval_id=request.id,
        tool_name=tool.name,
        args=args,
        summary=summary,
    )
    return {
        "approval_id": request.id,
        "tool_name": tool.name,
        "args": args,
        "summary": summary,
    }


async def _execute_tool(
    call: dict[str, Any], tool: Any, context: ToolContext, writer
) -> dict[str, Any]:
    name = tool.name
    call_id = str(call.get("id") or "")
    args = dict(call.get("args") or {})
    writer({"type": "tool_call", "name": name, "call_id": call_id, "args": args})
    try:
        data = await tool.run(context, **args)
        result = {"call_id": call_id, "name": name, "ok": True, "data": data if isinstance(data, dict) else {"value": data}}
    except ToolError as exc:
        result = {
            "call_id": call_id,
            "name": name,
            "ok": False,
            "data": {},
            "error": str(exc),
            "error_code": exc.code,
        }
    except Exception as exc:  # noqa: BLE001
        result = {
            "call_id": call_id,
            "name": name,
            "ok": False,
            "data": {},
            "error": f"{type(exc).__name__}: {exc}",
            "error_code": "tool_exception",
        }
    writer(
        {
            "type": "tool_result",
            "name": name,
            "call_id": call_id,
            "ok": bool(result["ok"]),
            "error": result.get("error", ""),
            "preview": _preview(result.get("data") if result["ok"] else None),
        }
    )
    return result


# ---------------------------------------------------------------------------
# 编排驱动：执行 intent_router 选中的编排型 Skill（确定性控制流，不消耗 think 调用）
# ---------------------------------------------------------------------------


async def skill_exec_node(state: AgentSubState) -> dict[str, Any]:
    """执行编排型 Skill：按 Skill 内代码顺序调用原子工具，跳过 Agent 自主规划。

    - 全步成功 → finalize（`orchestration="skill"`），由答复节点把 data 组织成话术；
    - 任一步失败 → reflect（仅失败时触发校验，符合"不引入新长尾"的目标）；
    - 命中需人工确认的写操作 → 落 pending approval + 确认卡片 → finalize。
    """
    writer = _get_writer()
    registry = _get_skills(state)
    name = _selected_skill(state)
    slots = dict(state.get("skill_slots") or {})

    skill = registry.try_get(name) if name else None
    if skill is None:
        # 编排技能缺失/被热卸载：回退 Agent 自主规划（保底可用）
        writer({"type": "agent_skill_missing", "skill": name})
        return {
            "skill_exec_decision": "think",
            "skill_name": "",
            "skill_slots": {},
            "orchestration": "agent",
        }

    context = SkillContext(
        phone=str(state.get("phone") or DEFAULT_DEMO_PHONE),
        workspace=str(state.get("workspace") or ""),
        approval_mode=str(state.get("approval_mode") or "inline"),
        tools=_get_tools(state),
    )
    writer({"type": "agent_skill_start", "skill": name, "slots": slots})

    try:
        result = await skill.run(context, **slots)
    except ConfirmationRequired as exc:
        confirmation = await _build_skill_confirmation(exc, context, writer)
        writer(
            {
                "type": "agent_confirm_required",
                "approval_id": confirmation["approval_id"],
                "tool_name": confirmation["tool_name"],
                "summary": confirmation["summary"],
            }
        )
        return {
            "orchestration": "skill",
            "skill_result": {"skill": name, "ok": False, "steps": [], "data": {}},
            "skill_steps": [],
            "tool_calls": [],
            "tool_results": [],
            "confirmation_request": confirmation,
            "skill_exec_decision": "finalize",
        }
    except ToolError as exc:
        result = {
            "skill": name,
            "ok": False,
            "steps": [],
            "data": {},
            "error": str(exc),
            "error_code": exc.code,
        }
    except Exception as exc:  # noqa: BLE001 —— 编排内部异常同样交给反思决策
        result = {
            "skill": name,
            "ok": False,
            "steps": [],
            "data": {},
            "error": f"{type(exc).__name__}: {exc}",
            "error_code": "skill_exception",
        }

    ok = bool(result.get("ok"))
    steps = list(result.get("steps") or [])
    call_id = f"sk-{name}"
    tool_results = [
        {
            "call_id": call_id,
            "name": name,
            "ok": ok,
            "data": dict(result.get("data") or {}) if ok else {},
            "error": "" if ok else str(result.get("error") or ""),
            "error_code": "" if ok else str(result.get("error_code") or "tool_error"),
        }
    ]
    writer(
        {
            "type": "agent_skill",
            "skill": name,
            "ok": ok,
            "steps": steps,
            "error": "" if ok else str(result.get("error") or ""),
        }
    )
    return {
        "orchestration": "skill",
        "skill_result": result,
        "skill_steps": steps,
        "tool_calls": [{"id": call_id, "name": name, "args": slots}],
        "tool_results": tool_results,
        "skill_exec_decision": "finalize" if ok else "reflect",
    }


def skill_exec_route(state: AgentSubState) -> str:
    return str(state.get("skill_exec_decision") or "finalize")


async def _build_skill_confirmation(
    exc: ConfirmationRequired, context: SkillContext, writer
) -> dict[str, Any]:
    """把编排抛出的 ConfirmationRequired 落成跨轮确认单（复用原子工具确认链路）。"""
    tools = context.tools if context.tools is not None else get_tool_registry()
    tool = tools.try_get(exc.tool_name)
    if tool is not None:
        call = {
            "id": f"call-{uuid4().hex[:8]}",
            "name": exc.tool_name,
            "args": dict(exc.tool_args),
        }
        return await _build_tool_confirmation(tool, _to_tool_context(context), call, writer)

    # 原子工具已被热卸载：退化为直接用编排给出的摘要落单
    summary = exc.summary or (
        f"即将为您办理「{exc.tool_name}」，参数：{json.dumps(exc.tool_args, ensure_ascii=False)}。"
        "请确认是否办理？（回复「确认」继续，回复「取消」放弃）"
    )
    request = ApprovalRequest(
        id=make_approval_request(command=summary, risk_reason="").id,
        command=summary,
        risk_reason=f"高危业务写操作：{exc.tool_name}",
        tool_name=exc.tool_name,
    )
    await save_pending_approval(
        context.workspace,
        approval_id=request.id,
        tool_name=exc.tool_name,
        args=dict(exc.tool_args),
        summary=summary,
    )
    return {
        "approval_id": request.id,
        "tool_name": exc.tool_name,
        "args": dict(exc.tool_args),
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# P4-14：反思校验节点（verifier 业务一致性改造）
# ---------------------------------------------------------------------------


async def reflect_node(state: AgentSubState) -> dict[str, Any]:
    writer = _get_writer()
    results = list(state.get("tool_results") or [])
    attempts = int(state.get("attempts", 0) or 0) + 1
    max_attempts = int(state.get("max_attempts", 2) or 2)
    all_ok = bool(results) and all(bool(item.get("ok")) for item in results)
    hard_failure = bool(results) and any(
        not item.get("ok") and item.get("error_code") in _FATAL_ERROR_CODES for item in results
    )

    decision = "pass"
    reason = ""
    hint = ""
    checks: list[dict[str, Any]] = []

    if not results:
        # 无工具结果（理论上不会走到：act 无确认单必有结果）——直接兜底
        decision, reason = "fallback", "no_tool_results"
    elif hard_failure:
        # 不可挽救的业务错误（号码/套餐不存在、审批拒绝）：不浪费重试
        decision, reason = "fallback", "business_unavailable"
    else:
        try:
            response = await create_model().ainvoke(
                [
                    SystemMessage(content=AGENT_REFLECT_PROMPT),
                    HumanMessage(content=_reflect_payload(state, results)),
                ]
            )
            parsed = _extract_json(str(getattr(response, "content", "") or "")) or {}
            decision = str(parsed.get("decision") or "").strip().lower()
            reason = str(parsed.get("reason") or "").strip()
            hint = str(parsed.get("hint") or "").strip()
            checks = _normalize_checks(parsed.get("checks"))
            if decision not in ("pass", "retry", "fallback"):
                decision = "pass" if all_ok else "retry"
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"
            writer({"type": "agent_reflect_error", "error": error})
            # 模型不可用时退回规则判定：全部成功即放行，否则可重试一次
            decision = "pass" if all_ok else "retry"
            reason = "reflect model unavailable, rule-based decision"

    if not all_ok and decision == "pass":
        # 存在失败工具时禁止放行
        decision = "retry"
        reason = reason or "some tools failed"
    if decision == "retry" and attempts >= max_attempts:
        decision = "fallback"
        reason = (reason or "retry limit reached") + " (attempts_exceeded)"
    if decision == "fallback" and not reason:
        reason = "reflect_rejected"

    writer(
        {
            "type": "agent_reflect",
            "decision": decision,
            "reason": reason,
            "hint": hint,
            "attempts": attempts,
            "checks": checks,
        }
    )
    return {
        "verify_decision": decision,
        "verify_reason": reason,
        "verify_hint": hint,
        "verification_checks": checks,
        "attempts": attempts,
    }


def reflect_route(state: AgentSubState) -> str:
    return str(state.get("verify_decision") or "fallback")


# 无需重试即可判定"业务不可完成"的错误码
_FATAL_ERROR_CODES = {
    "account_not_found",
    "package_not_found",
    "ticket_not_found",
    "same_package",
    "ambiguous_package",
    "invalid_enum",
    "approval_denied",
}


def _reflect_payload(state: AgentSubState, results: list[dict[str, Any]]) -> str:
    calls = [
        {"call_id": call.get("id", ""), "name": call.get("name", ""), "args": call.get("args", {})}
        for call in (state.get("tool_calls") or [])
    ]
    payload = {
        "用户诉求": state.get("query", ""),
        "工具调用": calls,
        "工具返回": [
            {
                "name": item.get("name", ""),
                "ok": item.get("ok", False),
                "data": item.get("data", {}),
                **({"error": item["error"]} if item.get("error") else {}),
            }
            for item in results
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


# ---------------------------------------------------------------------------
# 答复组织 / 兜底
# ---------------------------------------------------------------------------


async def finalize_node(state: AgentSubState) -> dict[str, Any]:
    """把工具结果组织成最终客服话术；确认卡片/取消/直接回复不调用 LLM。"""
    writer = _get_writer()

    confirmation = state.get("confirmation_request") or None
    if confirmation is not None:
        answer = str(confirmation.get("summary") or "")
        writer({"type": "agent_answer", "mode": "confirmation", "preview": answer[:200]})
        return {"answer": answer, "chat_response": answer, "final_answer": answer}

    preset = str(state.get("answer") or state.get("direct_answer") or "").strip()
    if preset:
        writer({"type": "agent_answer", "mode": "direct", "preview": preset[:200]})
        return {"answer": preset, "chat_response": preset, "final_answer": preset}

    results = list(state.get("tool_results") or [])
    messages = [
        SystemMessage(content=AGENT_RESPOND_PROMPT),
        HumanMessage(content=_respond_payload(state, results)),
    ]
    answer = ""
    error = ""
    try:
        response = await create_model().ainvoke(messages)
        answer = str(getattr(response, "content", "") or "").strip()
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
    if not answer:
        answer = AGENT_RESPOND_ERROR_REPLY
    writer(
        {
            "type": "agent_answer",
            "mode": "generated",
            "preview": answer[:200],
            "error": error,
            "tool_count": len(results),
        }
    )
    return {"answer": answer, "chat_response": answer, "final_answer": answer}


def agent_fallback_node(state: AgentSubState) -> dict[str, Any]:
    """P4-15：异常/校验失败超阈值的静态兜底话术。"""
    reason = str(state.get("verify_reason") or state.get("fallback_reason") or "attempts_exceeded")
    _get_writer()({"type": "agent_fallback", "reason": reason})
    return {
        "answer": AGENT_FALLBACK_REPLY,
        "chat_response": AGENT_FALLBACK_REPLY,
        "final_answer": AGENT_FALLBACK_REPLY,
        "fallback_reason": reason,
    }


def _respond_payload(state: AgentSubState, results: list[dict[str, Any]]) -> str:
    data_lines = []
    for item in results:
        if item.get("ok"):
            data_lines.append(
                f"工具 {item.get('name')} 返回：\n"
                + json.dumps(item.get("data", {}), ensure_ascii=False, indent=2, default=str)
            )
        else:
            data_lines.append(
                f"工具 {item.get('name')} 失败：{item.get('error', '')}"
                "（如无法挽救请礼貌建议联系 10000 号人工客服）"
            )
    # 四层组装：记忆 → 业务工具返回（证据层）→ 用户诉求（P5-9）
    return compose_layered_content(
        question=str(state.get("query", "")),
        memory_text=_agent_memory_text(state),
        extra_sections=["【业务工具返回（答复只能基于以下数据）】\n" + "\n\n".join(data_lines)],
    )


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _get_tools(state: AgentSubState) -> ToolRegistry:
    # 测试可通过 state["_tools"] 注入替身；生产取进程单例
    registry = state.get("_tools")
    if isinstance(registry, ToolRegistry):
        return registry
    return get_tool_registry()


def _get_skills(state: AgentSubState) -> SkillRegistry:
    # 测试可通过 state["_skills"] 注入替身；生产取进程单例
    registry = state.get("_skills")
    if isinstance(registry, SkillRegistry):
        return registry
    return get_skill_registry()


def _to_tool_context(context: SkillContext) -> ToolContext:
    """编排技能上下文 → 原子工具上下文（仅传递三个公共字段）。"""
    return ToolContext(
        phone=context.phone,
        workspace=context.workspace,
        approval_mode=context.approval_mode,
    )


def _normalize_checks(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    checks = []
    for item in raw:
        if isinstance(item, dict):
            checks.append(
                {
                    "name": str(item.get("name") or "check"),
                    "passed": bool(item.get("passed")),
                    "detail": str(item.get("detail") or ""),
                }
            )
    return checks


def _extract_json(text: str) -> dict[str, Any] | None:
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    raw = fenced.group(1) if fenced else text
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        parsed = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _preview(data: Any, limit: int = 240) -> str:
    if not isinstance(data, dict):
        return ""
    return json.dumps(data, ensure_ascii=False, default=str)[:limit]


def _get_writer():
    try:
        return get_stream_writer()
    except RuntimeError:
        return lambda _: None
