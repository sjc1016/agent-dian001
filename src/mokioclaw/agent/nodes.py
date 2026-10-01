"""阶段 4：Agent 深度推理子图节点（全部 async，LangGraph 调度器并发执行）。

拓扑见 :func:`mokioclaw.agent.workflow.build_agent_subgraph`：

    START → approval_entry ─┬─ think ─→ act（asyncio.gather 多工具并行）
                            │              └→ reflect ─┬─ pass → finalize → END
                            │              ┌───────────┼─ retry → think（attempts+1）
                            ├─ act（确认恢复）──────────┴─ fallback → agent_fallback → END
                            └─ finalize（取消/无需工具/确认卡片）→ END

关键设计：
- 思考节点通过 Skill 注册中心实时 ``bind_tools``（热插拔工具当轮即可被发现）；
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

from mokioclaw.core.approval import ApprovalRequest, make_approval_request
from mokioclaw.prompts.agent import (
    AGENT_FALLBACK_REPLY,
    AGENT_REFLECT_ERROR_REPLY,
    AGENT_REFLECT_PROMPT,
    AGENT_RESPOND_ERROR_REPLY,
    AGENT_RESPOND_PROMPT,
    AGENT_THINK_ERROR_REPLY,
    AGENT_THINK_PROMPT,
    APPROVAL_CANCELLED_REPLY,
)
from mokioclaw.providers.openai_provider import create_model
from mokioclaw.skills.base import SkillContext, SkillError
from mokioclaw.skills.business_store import (
    DEFAULT_DEMO_PHONE,
    save_pending_approval,
    update_pending_approval,
)
from mokioclaw.skills.registry import SkillRegistry, get_registry
from mokioclaw.agent.state import AgentSubState

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
        return {"entry_decision": "think"}

    approval_id = str(pending.get("approval_id") or "")
    if resolution == "cancelled":
        if approval_id:
            await update_pending_approval(approval_id, "cancelled")
        writer(
            {
                "type": "approval_cancelled",
                "approval_id": approval_id,
                "skill_name": pending.get("skill_name", ""),
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
            "name": str(pending.get("skill_name") or ""),
            "args": dict(pending.get("args") or {}),
            "approved": True,
        }
        if approval_id:
            await update_pending_approval(approval_id, "approved")
        writer(
            {
                "type": "approval_resumed",
                "approval_id": approval_id,
                "skill_name": call["name"],
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

    # 用户没有明确确认/取消而是说了别的内容：旧确认单作废，按新诉求重新思考
    if approval_id:
        await update_pending_approval(approval_id, "cancelled")
    writer({"type": "approval_expired", "approval_id": approval_id})
    return {"entry_decision": "think", "pending_approval": None}


def approval_entry_route(state: AgentSubState) -> str:
    return str(state.get("entry_decision") or "think")


# ---------------------------------------------------------------------------
# P4-12：思考节点（planner 客服域改造）
# ---------------------------------------------------------------------------


async def think_node(state: AgentSubState) -> dict[str, Any]:
    """分析对话状态决定本轮工具调用；无需工具时直接给出追问/解释文本。"""
    writer = _get_writer()
    registry = _get_registry(state)
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
        if registry.try_get(name) is None:
            unknown_names.append(name)
            continue
        valid_calls.append(
            {"id": str(raw.get("id") or f"call-{uuid4().hex[:8]}"), "name": name, "args": args}
        )

    if unknown_names:
        writer({"type": "agent_unknown_skill", "names": unknown_names})

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


def _build_think_messages(state: AgentSubState, registry: SkillRegistry) -> list[Any]:
    system_prompt = AGENT_THINK_PROMPT
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

    sections = [f"【用户诉求】\n{state.get('query', '')}"]
    session_context = str(state.get("session_context") or "").strip()
    if session_context:
        sections.append("【会话记忆（近期对话，仅供理解指代，勿泄露原始 JSON）】\n" + session_context)
    phone = str(state.get("phone") or DEFAULT_DEMO_PHONE)
    sections.append(f"【当前会话绑定号码】\n{phone}（用户未显式提供号码时查询/办理该号码）")
    hint = str(state.get("verify_hint") or "").strip()
    if hint:
        sections.append(
            f"【上轮反思校验未通过，请据此修正】\n{hint}\n"
            "（可修正参数后重新调用工具，或补充调用其他工具核实）"
        )
    return [SystemMessage(content=system_prompt), HumanMessage(content="\n\n".join(sections))]


# ---------------------------------------------------------------------------
# P4-13：工具编排节点（单轮多工具并行 asyncio.gather；P4-16 审批短路）
# ---------------------------------------------------------------------------


async def act_node(state: AgentSubState) -> dict[str, Any]:
    writer = _get_writer()
    registry = _get_registry(state)
    context = SkillContext(
        phone=str(state.get("phone") or DEFAULT_DEMO_PHONE),
        workspace=str(state.get("workspace") or ""),
        approval_mode=str(state.get("approval_mode") or "inline"),
    )
    calls = list(state.get("tool_calls") or [])

    executable: list[tuple[dict[str, Any], Any]] = []
    confirmation: dict[str, Any] | None = None
    results: list[dict[str, Any]] = []

    for call in calls:
        skill = registry.try_get(str(call.get("name") or ""))
        if skill is None:
            results.append(
                {
                    "call_id": call.get("id", ""),
                    "name": call.get("name", ""),
                    "ok": False,
                    "data": {},
                    "error": f"工具 {call.get('name')} 未注册（可能已被热卸载）",
                    "error_code": "unknown_skill",
                }
            )
            continue

        if skill.access == "write" and skill.requires_confirmation and not call.get("approved"):
            if context.approval_mode == "auto":
                call["approved"] = True
                executable.append((call, skill))
            elif context.approval_mode == "deny":
                results.append(
                    {
                        "call_id": call.get("id", ""),
                        "name": skill.name,
                        "ok": False,
                        "data": {},
                        "error": "当前审批策略为 deny，写操作未获授权，已拒绝执行。",
                        "error_code": "approval_denied",
                    }
                )
            else:
                # inline：落 pending approval + 返回确认卡片，本轮不执行该写操作
                confirmation = await _build_confirmation(skill, context, call, writer)
        else:
            executable.append((call, skill))

    if executable:
        coros = [_execute_skill(call, skill, context, writer) for call, skill in executable]
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
                "skill_name": confirmation["skill_name"],
                "summary": confirmation["summary"],
            }
        )
    return update


def act_route(state: AgentSubState) -> str:
    if state.get("confirmation_request"):
        return "finalize"
    return "reflect"


async def _build_confirmation(skill: Any, context: SkillContext, call: dict[str, Any], writer) -> dict[str, Any]:
    args = dict(call.get("args") or {})
    if hasattr(skill, "confirmation_summary"):
        summary = skill.confirmation_summary(context, args)
    else:
        summary = (
            f"即将为您办理「{skill.name}」，参数：{json.dumps(args, ensure_ascii=False)}。"
            "请确认是否办理？（回复「确认」继续，回复「取消」放弃）"
        )
    # 复用现有 ApprovalRequest 结构（tool_name 字段承载 Skill 名）
    request = ApprovalRequest(
        id=make_approval_request(command=summary, risk_reason="").id,
        command=summary,
        risk_reason=f"高危业务写操作：{skill.name}",
        tool_name=skill.name,
    )
    await save_pending_approval(
        context.workspace,
        approval_id=request.id,
        skill_name=skill.name,
        args=args,
        summary=summary,
    )
    return {
        "approval_id": request.id,
        "skill_name": skill.name,
        "args": args,
        "summary": summary,
    }


async def _execute_skill(call: dict[str, Any], skill: Any, context: SkillContext, writer) -> dict[str, Any]:
    name = skill.name
    call_id = str(call.get("id") or "")
    args = dict(call.get("args") or {})
    writer({"type": "skill_call", "name": name, "call_id": call_id, "args": args})
    try:
        data = await skill.run(context, **args)
        result = {"call_id": call_id, "name": name, "ok": True, "data": data if isinstance(data, dict) else {"value": data}}
    except SkillError as exc:
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
            "error_code": "skill_exception",
        }
    writer(
        {
            "type": "skill_result",
            "name": name,
            "call_id": call_id,
            "ok": bool(result["ok"]),
            "error": result.get("error", ""),
            "preview": _preview(result.get("data") if result["ok"] else None),
        }
    )
    return result


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
    sections = [f"【用户诉求】\n{state.get('query', '')}"]
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
    sections.append("【业务工具返回（答复只能基于以下数据）】\n" + "\n\n".join(data_lines))
    session_context = str(state.get("session_context") or "").strip()
    if session_context:
        sections.append("【会话记忆】\n" + session_context)
    return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _get_registry(state: AgentSubState) -> SkillRegistry:
    # 测试可通过 state["_registry"] 注入替身；生产取进程单例
    registry = state.get("_registry")  # type: ignore[typeddict-item]
    if isinstance(registry, SkillRegistry):
        return registry
    return get_registry()


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
