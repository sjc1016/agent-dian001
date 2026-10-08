from __future__ import annotations

import json
import os
import re
from typing import Any

from dotenv import load_dotenv
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.config import get_stream_writer
from langgraph.graph.message import REMOVE_ALL_MESSAGES

from congclaw.graph.memory import (
    assemble_layered_messages,
    build_customer_memory,
    build_layered_memory,
    customer_memory_event,
    format_layered_memory_for_prompt,
    memory_event,
    persist_history_summary,
    render_memory_sections,
)
from congclaw.graph.state import CongGraphState, TodoItem, VerificationCheck
from congclaw.agent.nodes import detect_approval_resolution
from congclaw.agent.workflow import build_agent_subgraph
from congclaw.faq.gate import evaluate_sediment_gate
from congclaw.faq.workflow import build_faq_sediment_subgraph
from congclaw.prompts.agent import AGENT_FALLBACK_REPLY
from congclaw.prompts.intent import (
    CLARIFY_MAX_ROUNDS,
    CLARIFY_PROMPT,
    FALLBACK_TEMPLATES,
    INTENT_CATEGORIES,
    INTENT_ROUTER_PROMPT,
    UNKNOWN_CONFIDENCE_FLOOR,
    UNKNOWN_MAX_STREAK,
)
from congclaw.prompts.memory import QUERY_REWRITE_PROMPT
from congclaw.prompts.stage3 import PLANNER_PROMPT, VERIFIER_PROMPT
from congclaw.prompts.stage4 import CONTEXT_COMPRESSION_PROMPT
from congclaw.providers.openai_provider import create_model
from congclaw.rag.workflow import build_rag_subgraph
from congclaw.skills.business_store import (
    DEFAULT_DEMO_PHONE,
    get_pending_approval,
    update_pending_approval,
)
from congclaw.tools.todo_tool import persist_todos, write_todos


DEFAULT_CONTEXT_TOKEN_LIMIT = 400000

DEFAULT_TODOS = [
    "Clarify the customer's request and required slots.",
    "Handle the request via retrieval or business skills.",
    "Reflect on and verify the response before finishing.",
]


async def query_rewrite_node(state: CongGraphState) -> dict[str, Any]:
    """P5-1/P5-2/P5-3：指代消解与查询重写，位于 intent_router 之前。

    结合短期会话窗口 + 长期用户摘要，把"那这个多少钱/帮我办这个"这类
    指代/省略输入改写为脱离上下文也完整的独立问句；首轮无历史窗口、
    空输入或模型判定无指代时原样透传（不改变后续意图分流）。

    同时承担四层记忆的首次装配：预渲染 memory_context 供 RAG/Agent
    子图复用（子图与主图状态解耦，只接收渲染好的记忆文本）。
    """
    writer = _get_writer()
    task = str(state.get("task") or "")
    memory = build_customer_memory(state, node="query_rewrite")
    memory_text = render_memory_sections(memory)
    writer(customer_memory_event(memory, node="query_rewrite"))

    base_update = {
        "rewritten_task": task,
        "rewrite_changed": False,
        "rewrite_reason": "",
        "memory_context": memory_text,
    }
    stripped = task.strip()
    has_window = bool(memory["working_memory"]["short_term_window"])
    if not stripped:
        writer({"type": "query_rewrite", "changed": False, "original": task, "rewritten": task,
                "reason": "empty input, passthrough"})
        return base_update
    if not has_window:
        # 首轮（无会话历史）不存在可消解的指代，跳过 LLM 直接透传，省一次模型调用
        writer({"type": "query_rewrite", "changed": False, "original": task, "rewritten": task,
                "reason": "no conversation window, passthrough"})
        return base_update

    rewritten, reason, error = task, "", ""
    try:
        response = await create_model().ainvoke(
            assemble_layered_messages(
                QUERY_REWRITE_PROMPT, state, question=task, node="query_rewrite"
            )
        )
        changed, rewritten, reason = _parse_rewrite_result(
            str(getattr(response, "content", "") or ""), task
        )
    except Exception as exc:  # noqa: BLE001 —— 重写模型异常绝不阻断主链路
        changed, rewritten, reason = False, task, "rewrite model error, passthrough"
        error = f"{type(exc).__name__}: {exc}"
        writer({"type": "query_rewrite_error", "error": error})

    writer(
        {
            "type": "query_rewrite",
            "changed": changed,
            "original": task,
            "rewritten": rewritten,
            "reason": reason,
        }
    )
    return {
        **base_update,
        "rewritten_task": rewritten,
        "rewrite_changed": changed,
        "rewrite_reason": reason,
    }


def _parse_rewrite_result(raw: str, original: str) -> tuple[bool, str, str]:
    """解析重写模型输出 → (changed, rewritten, reason)，任何异常形态都安全透传。"""
    parsed = _extract_json(raw)
    if parsed is not None:
        rewritten = str(parsed.get("rewritten") or "").strip()
        reason = str(parsed.get("reason") or "").strip()
        changed = bool(parsed.get("changed"))
        if not rewritten:
            return False, original, reason or "model returned empty rewrite"
        if not changed or rewritten == original.strip():
            return False, original, reason or "no coreference, passthrough"
        return True, rewritten, reason or "coreference resolved"
    # 兼容个别模型不遵守 JSON 协议直接返回改写句子的情况
    plain = raw.strip()
    if plain and "{" not in plain and plain != original.strip():
        return True, plain, "plain-text rewrite"
    return False, original, "unparseable output, passthrough"


async def intent_router_node(state: CongGraphState) -> dict[str, Any]:
    """阶段 2：五类意图识别 + 追问/unknown 计数 + 强制兜底路由（阶段 4 起原生 async）。

    类别：rag_query / agent_service / clarify / irrelevant / unknown；
    路由：rag_answer / agent_loop / clarify / fallback（unknown 未达阈值先追问）。

    阶段 4：若该会话存在未决的写操作人工确认单，用户回复"确认/取消"时短路进入
    Agent 子图恢复执行；用户转而提出其它诉求时先作废旧确认单再正常分类。
    """
    writer = _get_writer()

    # P4-16：未决人工确认单短路（规则判定优先，确定可测且不额外消耗 LLM）
    task_text = str(state.get("task") or "")
    workspace = str(state.get("workspace") or "")
    pending = await _load_pending_approval(workspace)
    resolution = detect_approval_resolution(task_text) if pending is not None else "unknown"
    if pending is not None:
        if resolution in ("confirmed", "cancelled"):
            writer(
                {
                    "type": "intent_decision",
                    "route": "agent_loop",
                    "category": "agent_service",
                    "reason": f"pending approval {resolution} → resume in agent subgraph",
                    "confidence": 1.0,
                    "clarify_count": 0,
                    "unknown_count": 0,
                }
            )
            return {
                "intent_category": "agent_service",
                "intent_route": "agent_loop",
                "intent_reason": "pending_approval_resume",
                "intent_confidence": 1.0,
                "pending_slots": [],
                "fallback_reason": "",
                "clarify_count": 0,
                "unknown_count": 0,
                "pending_approval": pending,
                "approval_resolution": resolution,
            }
        # 用户说了新诉求：旧确认单作废，交由正常意图分类处理
        await _cancel_pending_approval(pending)
        writer(
            {
                "type": "approval_expired",
                "approval_id": str(pending.get("approval_id") or ""),
                "reason": "user started a new request before confirming",
            }
        )

    category = "unknown"
    reason = "router fallback: default to unknown"
    confidence = 0.0
    pending_slots: list[str] = []
    # 阶段 5：意图分类输入使用重写后的独立问句（指代已消解）；审批短路判定已用原文完成
    question = str(state.get("rewritten_task") or task_text or "")
    try:
        response = await create_model().ainvoke(
            assemble_layered_messages(
                INTENT_ROUTER_PROMPT,
                state,
                question=question,
                node="intent_router",
            )
        )
        parsed = _extract_json(str(response.content)) or {}
        candidate = str(parsed.get("category", "")).strip().lower()
        confidence = _coerce_confidence(parsed.get("confidence"))
        if candidate in INTENT_CATEGORIES:
            category = candidate
            reason = str(parsed.get("reason") or "")
            raw_slots = parsed.get("missing_slots")
            if isinstance(raw_slots, list):
                pending_slots = [str(slot).strip() for slot in raw_slots if str(slot).strip()][:5]
        else:
            reason = str(parsed.get("reason") or "router returned invalid category")
        if confidence < UNKNOWN_CONFIDENCE_FLOOR:
            category = "unknown"
            reason = f"{reason} (low confidence)".strip()
    except Exception as exc:
        reason = f"router error: {type(exc).__name__}: {exc}"

    if category in ("clarify", "unknown") and not pending_slots:
        # 追问延续：上一轮识别出的待补槽位继续有效，避免追问失去靶点
        pending_slots = [str(slot) for slot in (state.get("pending_slots") or []) if str(slot).strip()]

    # 计数维护：明确业务即清零（连续语义）；clarify 由 clarify_node 递增；unknown 由本节点递增
    clarify_count = int(state.get("clarify_count", 0) or 0)
    unknown_count = int(state.get("unknown_count", 0) or 0)
    if category == "unknown":
        unknown_count += 1
    elif category in ("rag_query", "agent_service", "irrelevant"):
        clarify_count = 0
        unknown_count = 0

    route, fallback_reason = _route_for(category, clarify_count, unknown_count)
    event = {
        "type": "intent_decision",
        "route": route,
        "category": category,
        "reason": reason,
        "confidence": confidence,
        "clarify_count": clarify_count,
        "unknown_count": unknown_count,
        "original_input": task_text,
        "rewritten_input": question,
        "rewrite_changed": bool(state.get("rewrite_changed")),
    }
    writer(event)
    return {
        "intent_category": category,
        "intent_route": route,
        "intent_reason": reason,
        "intent_confidence": confidence,
        "pending_slots": pending_slots,
        "fallback_reason": fallback_reason,
        "clarify_count": clarify_count,
        "unknown_count": unknown_count,
    }


def _route_for(category: str, clarify_count: int, unknown_count: int) -> tuple[str, str]:
    """意图类别 → 图节点路由；达到阈值时强制兜底（P2-6）并给出兜底原因。"""
    if category == "irrelevant":
        return "fallback", "irrelevant_request"
    if clarify_count >= CLARIFY_MAX_ROUNDS:
        return "fallback", "clarify_exceeded"
    if unknown_count >= UNKNOWN_MAX_STREAK:
        return "fallback", "unknown_streak"
    if category in ("clarify", "unknown"):
        return "clarify", ""
    return ("rag_answer", "") if category == "rag_query" else ("agent_loop", "")


def intent_route_fn(state: CongGraphState) -> str:
    return str(state.get("intent_route") or "fallback")


async def clarify_node(state: CongGraphState) -> dict[str, Any]:
    """P2-4：按 pending_slots 生成追问话术，clarify_count += 1（阶段 4 起原生 async）。"""
    writer = _get_writer()
    pending_slots = [str(slot) for slot in (state.get("pending_slots") or []) if str(slot).strip()]
    question = str(state.get("task") or "")
    extra_sections = []
    if pending_slots:
        extra_sections.append("待补槽位：" + "、".join(pending_slots[:3]))
    question_text = ""
    try:
        response = await create_model().ainvoke(
            assemble_layered_messages(
                CLARIFY_PROMPT,
                state,
                question=question,
                node="clarify",
                extra_sections=extra_sections,
            )
        )
        question_text = str(getattr(response, "content", "") or "").strip()
    except Exception as exc:
        writer({"type": "clarify_model_error", "error": f"{type(exc).__name__}: {exc}"})
    if not question_text:
        question_text = _default_clarify_question(pending_slots)
    clarify_count = int(state.get("clarify_count", 0) or 0) + 1
    event = {
        "type": "clarify_question",
        "question": question_text,
        "pending_slots": pending_slots,
        "clarify_count": clarify_count,
        "category": state.get("intent_category", "clarify"),
    }
    writer(event)
    return {
        "clarify_count": clarify_count,
        "clarify_question": question_text,
        "chat_response": question_text,
        "final_answer": question_text,
    }


def fallback_node(state: CongGraphState) -> dict[str, Any]:
    """P2-5：兜底话术（说明服务边界 + 引导回四类业务），记录 fallback_reason。"""
    writer = _get_writer()
    reason = str(state.get("fallback_reason") or "")
    if not reason:
        if int(state.get("clarify_count", 0) or 0) >= CLARIFY_MAX_ROUNDS:
            reason = "clarify_exceeded"
        elif int(state.get("unknown_count", 0) or 0) >= UNKNOWN_MAX_STREAK:
            reason = "unknown_streak"
        else:
            reason = "irrelevant_request"
    reply = FALLBACK_TEMPLATES.get(reason, FALLBACK_TEMPLATES["irrelevant_request"])
    event = {"type": "fallback_reply", "reason": reason, "reply": reply}
    writer(event)
    return {"fallback_reason": reason, "chat_response": reply, "final_answer": reply}


async def rag_answer_node(state: CongGraphState) -> dict[str, Any]:
    """P3-21：RAG 检索子图挂载点（阶段 4 起与主图同为原生 async）。

    主图与子图状态解耦：问句进、答案+来源出。主图由 astream 原生驱动，
    这里直接 async for 子图 astream，并把子图 custom 事件（两路召回/融合分/
    精排分/证据门...）实时转发到主图流，接入 trace。
    """
    writer = _get_writer()
    task = str(state.get("task") or "")
    # 阶段 5：检索与意图分流都使用重写后的独立问句；答案展示仍回传用户原始问句
    query = str(state.get("rewritten_task") or task)
    sub_input = {
        "query": query,
        "original_query": task,
        "rag_attempts": 0,
        "session_context": str(state.get("session_context") or ""),
        "memory_context": _memory_context(state),
    }
    writer({"type": "rag_start", "query": query, "original_query": task})

    try:
        result: dict[str, Any] = {}
        node_sequence: list[str] = []
        async for mode, chunk in build_rag_subgraph().astream(
            sub_input, stream_mode=["updates", "custom"]
        ):
            if mode == "custom":
                writer(chunk)
            elif isinstance(chunk, dict):
                for node_name, update in chunk.items():
                    node_sequence.append(str(node_name))
                    if isinstance(update, dict):
                        result.update(update)
        writer({"type": "rag_trace", "node_sequence": node_sequence})
    except Exception as exc:  # 子图整体异常不打断对话，降级为话术兜底
        error = f"{type(exc).__name__}: {exc}"
        writer({"type": "rag_error", "error": error})
        reply = (
            "抱歉，知识库检索服务暂时异常，暂时无法回答该问题，"
            "请稍后再试或拨打 10000 号人工客服。"
        )
        return {"chat_response": reply, "final_answer": reply, "sources": []}

    answer = str(result.get("answer") or "")
    sources = list(result.get("sources") or [])
    metadata = dict(state.get("metadata") or {})
    if result.get("fallback_reason"):
        metadata["rag_fallback_reason"] = result["fallback_reason"]
    writer(
        {
            "type": "rag_finished",
            "has_answer": bool(answer),
            "sources_count": len(sources),
            "rag_attempts": int(result.get("rag_attempts", 0) or 0),
        }
    )
    return {
        "chat_response": answer,
        "final_answer": answer,
        "sources": sources,
        "metadata": metadata,
    }


async def agent_loop_node(state: CongGraphState) -> dict[str, Any]:
    """P4-12~P4-16：Agent 深度推理子图挂载点（替换阶段 2 占位）。

    问句进、业务答复出：思考（bind_tools 六类 Skill）→ 多工具并行执行
    → 反思校验（失败按 attempts 重试/兜底）→ 中文答复；写操作的人工确认
    卡片与跨轮"确认/取消"恢复全部在子图内完成，主图只透传事件与轨迹。
    """
    writer = _get_writer()
    task = str(state.get("task") or "")
    workspace = str(state.get("workspace") or "")
    # 阶段 5：传给 Agent 的诉求为重写后的完整问句（"帮我办这个"→含套餐名）
    query = str(state.get("rewritten_task") or task)

    pending = state.get("pending_approval")
    if pending is None:
        pending = await _load_pending_approval(workspace)
    resolution = str(state.get("approval_resolution") or "")
    if pending is not None and not resolution:
        resolution = detect_approval_resolution(task)

    sub_input: dict[str, Any] = {
        "query": query,
        "session_context": str(state.get("session_context") or ""),
        "memory_context": _memory_context(state),
        "phone": str(state.get("phone") or DEFAULT_DEMO_PHONE),
        "workspace": workspace,
        "approval_mode": str(state.get("approval_mode") or "inline"),
        "pending_approval": pending,
        "approval_resolution": resolution or "unknown",
        "attempts": 0,
        "max_attempts": int(state.get("max_attempts", 3) or 3),
    }
    writer(
        {
            "type": "agent_start",
            "query": query,
            "original_query": task,
            "has_pending_approval": pending is not None,
            "approval_resolution": sub_input["approval_resolution"],
        }
    )

    try:
        result: dict[str, Any] = {}
        node_sequence: list[str] = []
        tool_traces: list[dict[str, Any]] = []
        async for mode, chunk in build_agent_subgraph().astream(
            sub_input, stream_mode=["updates", "custom"]
        ):
            if mode == "custom":
                writer(chunk)
                if isinstance(chunk, dict) and chunk.get("type") in ("skill_call", "skill_result"):
                    tool_traces.append(chunk)
            elif isinstance(chunk, dict):
                for node_name, update in chunk.items():
                    node_sequence.append(str(node_name))
                    if isinstance(update, dict):
                        result.update(update)
        writer({"type": "agent_trace", "node_sequence": node_sequence})
    except Exception as exc:  # 子图整体异常不打断对话，静态话术转人工兜底
        error = f"{type(exc).__name__}: {exc}"
        writer({"type": "agent_error", "error": error})
        reply = AGENT_FALLBACK_REPLY
        return {
            "chat_response": reply,
            "final_answer": reply,
            "fallback_reason": "agent_subgraph_error",
            "tool_traces": [],
        }

    answer = str(result.get("answer") or "")
    metadata = dict(state.get("metadata") or {})
    if result.get("fallback_reason"):
        metadata["agent_fallback_reason"] = result["fallback_reason"]
    if result.get("verify_decision"):
        metadata["agent_verify_decision"] = result["verify_decision"]
        metadata["agent_verify_reason"] = result.get("verify_reason", "")
    confirmation = result.get("confirmation_request")
    if confirmation:
        metadata["pending_confirmation"] = {
            "approval_id": confirmation.get("approval_id", ""),
            "skill_name": confirmation.get("skill_name", ""),
            "summary": confirmation.get("summary", ""),
        }
    writer(
        {
            "type": "agent_finished",
            "has_answer": bool(answer),
            "fallback_reason": str(result.get("fallback_reason") or ""),
            "confirmation_required": confirmation is not None,
            "tool_trace_count": len(tool_traces),
        }
    )
    return {
        "chat_response": answer,
        "final_answer": answer,
        "fallback_reason": str(result.get("fallback_reason") or state.get("fallback_reason") or ""),
        "metadata": metadata,
        "tool_traces": tool_traces,
    }


def sediment_route(state: CongGraphState) -> str:
    """阶段 7：四类业务分支之后的条件边——本轮是否值得沉淀。

    规则门控不通过的回合直接到 END，既不进入 faq_sediment 节点、也不产生
    任何 LLM 调用，因此绝大多数对话不会因沉淀能力而附加延迟。
    """
    passed, _ = evaluate_sediment_gate(state)
    return "faq_sediment" if passed else "end"


async def faq_sediment_node(state: CongGraphState) -> dict[str, Any]:
    """阶段 7：常见问答沉淀子图挂载点。

    把「用户问题 → 解决方案」沉淀为可复用的 FAQ 条目（默认 draft 待人工审核）。
    这是答复完成后的旁路副作用节点：子图整体异常只写 trace 与状态，绝不打断
    已完成的客服回合；与 ``rag_answer_node`` / ``agent_loop_node`` 一样，
    通过 ``astream`` 驱动子图并把 custom 事件实时转发进 trace。
    """
    writer = _get_writer()
    sub_input: dict[str, Any] = {
        "task": str(state.get("task") or ""),
        "rewritten_task": str(state.get("rewritten_task") or ""),
        "route": str(state.get("intent_route") or ""),
        "category": str(state.get("intent_category") or ""),
        "confidence": float(state.get("intent_confidence", 0.0) or 0.0),
        "final_answer": str(state.get("final_answer") or state.get("chat_response") or ""),
        "sources": list(state.get("sources") or []),
        "tool_traces": list(state.get("tool_traces") or []),
        "recent_turns": list(state.get("recent_turns") or []),
        "session_id": str(state.get("session_id") or ""),
        "session_turn": int(state.get("session_turn", 0) or 0),
        "candidates": [],
    }
    writer({"type": "faq_sediment_start", "route": sub_input["route"]})

    try:
        result: dict[str, Any] = {}
        node_sequence: list[str] = []
        async for mode, chunk in build_faq_sediment_subgraph().astream(
            sub_input, stream_mode=["updates", "custom"]
        ):
            if mode == "custom":
                writer(chunk)
            elif isinstance(chunk, dict):
                for node_name, update in chunk.items():
                    node_sequence.append(str(node_name))
                    if isinstance(update, dict):
                        result.update(update)
        writer({"type": "faq_sediment_trace", "node_sequence": node_sequence})
    except Exception as exc:  # noqa: BLE001 —— 沉淀失败不影响本轮会话
        error = f"{type(exc).__name__}: {exc}"
        writer({"type": "faq_sediment_error", "stage": "subgraph", "error": error})
        return {"faq_sediment_action": "error", "faq_entry_id": "", "faq_sediment_reason": error}

    action = str(result.get("action") or "skip")
    reason = str(result.get("reason") or result.get("gate_reason") or "")
    writer(
        {
            "type": "faq_sediment_finished",
            "action": action,
            "faq_id": str(result.get("faq_id") or ""),
            "persisted": bool(result.get("persisted")),
        }
    )
    return {
        "faq_sediment_action": action,
        "faq_entry_id": str(result.get("faq_id") or ""),
        "faq_sediment_reason": reason,
    }


async def _load_pending_approval(workspace: str) -> dict[str, Any] | None:
    """读取会话未决人工确认单（DB 异常不阻断主流程，按无确认单处理）。"""
    if not workspace:
        return None
    try:
        return await get_pending_approval(workspace)
    except Exception:  # noqa: BLE001
        return None


async def _cancel_pending_approval(pending: dict[str, Any]) -> None:
    approval_id = str(pending.get("approval_id") or "")
    if not approval_id:
        return
    try:
        await update_pending_approval(approval_id, "cancelled")
    except Exception:  # noqa: BLE001
        return


def _memory_context(state: CongGraphState) -> str:
    """获取预渲染记忆层文本；query_rewrite 已渲染则直接复用，否则现场构建（直连子图场景兜底）。"""
    existing = str(state.get("memory_context") or "").strip()
    if existing:
        return existing
    return render_memory_sections(build_customer_memory(state, node="graph"))


def _default_clarify_question(pending_slots: list[str]) -> str:
    if pending_slots:
        return "为了更好地为您办理，请补充：" + "、".join(pending_slots[:3]) + "。"
    return (
        "请问您想咨询或办理哪项业务呢？例如：查询话费余额、了解或变更套餐、"
        "宽带/手机故障报修、资费规则咨询。"
    )


def planner_node(state: CongGraphState) -> dict[str, Any]:
    writer = _get_writer()
    working_state: CongGraphState = {**state}
    if not working_state.get("todos"):
        _apply_plan(working_state, _default_plan(working_state["task"]))
        persist_todos(
            working_state["runtime"],
            working_state.get("todos", []),
            working_state.get("acceptance_criteria", []),
            working_state.get("verification_commands", []),
            working_state.get("plan_summary", ""),
        )

    memory = build_layered_memory(working_state, node="planner")
    writer(memory_event(memory, node="planner"))
    model = create_model()
    planner = model.bind_tools(_build_planner_tools(working_state, writer))
    messages: list[Any] = [
        SystemMessage(content=PLANNER_PROMPT),
        HumanMessage(content=_planner_input(working_state, memory)),
    ]
    produced_messages: list[Any] = []

    writer(
        {
            "type": "plan_snapshot",
            "node": "planner",
            "plan_summary": working_state.get("plan_summary", ""),
            "todos": working_state.get("todos", []),
            "verification_commands": working_state.get("verification_commands", []),
            "attempts": working_state.get("attempts", 0),
        }
    )

    for _ in range(8):
        response = planner.invoke(messages)
        produced_messages.append(response)
        messages.append(response)
        tool_calls = getattr(response, "tool_calls", None) or []
        if not tool_calls:
            break
        for call in tool_calls:
            tool_message = _execute_planner_tool(working_state, writer, call)
            produced_messages.append(tool_message)
            messages.append(tool_message)
    else:
        produced_messages.append(AIMessage(content="planner stopped after the maximum supervisor tool loop count."))

    metadata = dict(working_state.get("metadata", {}))
    metadata["planner_raw"] = _last_ai_content(produced_messages)
    final_memory = build_layered_memory(working_state, node="planner")
    return {
        "plan_summary": working_state.get("plan_summary", ""),
        "todos": working_state.get("todos", []),
        "acceptance_criteria": working_state.get("acceptance_criteria", []),
        "verification_commands": working_state.get("verification_commands", []),
        "research_notes": working_state.get("research_notes", ""),
        "sources": working_state.get("sources", []),
        "agent_handoffs": working_state.get("agent_handoffs", []),
        "code_agent_summary": working_state.get("code_agent_summary", ""),
        "last_actor_summary": working_state.get("code_agent_summary", ""),
        "messages": produced_messages,
        "memory_snapshot": final_memory,
        "history_summary": final_memory.get("history_summary_store", {}).get("history_summary", ""),
        "metadata": metadata,
        "context_next_node": "verifier",
    }


def verifier_node(state: CongGraphState) -> dict[str, Any]:
    writer = _get_writer()
    memory = build_layered_memory(state, node="verifier")
    writer(memory_event(memory, node="verifier"))
    writer(
        {
            "type": "plan_snapshot",
            "node": "verifier",
            "plan_summary": state.get("plan_summary", ""),
            "todos": state.get("todos", []),
            "verification_commands": state.get("verification_commands", []),
        }
    )

    model = create_model()
    # 业务 Skill 工具将在阶段 4 接入反思校验节点；阶段 0 暂不绑定任何工具。
    verifier = model
    messages: list[Any] = [
        SystemMessage(content=VERIFIER_PROMPT),
        HumanMessage(content=_verifier_input(state, memory)),
    ]
    produced_messages: list[Any] = []
    tool_events: list[dict[str, Any]] = []

    # 阶段 0：反思节点不绑定工具，直接要求模型返回校验 JSON；
    # 阶段 4 会在这里恢复“调用业务 Skill → 校验结果”的工具循环。
    response = verifier.invoke(messages)
    produced_messages.append(response)

    parsed = _extract_json(_last_ai_content(produced_messages)) or {
        "passed": False,
        "reason": "Verifier did not return valid JSON.",
        "checks": [
            {
                "name": "verifier_json",
                "passed": False,
                "detail": _last_ai_content(produced_messages)[:800],
            }
        ],
        "recommended_next_instruction": "Return valid verifier JSON after inspecting the result.",
    }
    checks = _normalize_checks(parsed.get("checks"))
    passed = bool(parsed.get("passed"))
    reason = str(parsed.get("reason") or "")
    recommended = str(parsed.get("recommended_next_instruction") or "")
    attempts = state.get("attempts", 0) + 1
    todos = [dict(todo) for todo in state.get("todos", [])]
    if passed:
        todos = [
            {
                **todo,
                "status": "completed" if todo.get("status") != "blocked" else todo.get("status", "blocked"),
                "note": todo.get("note") or "verified",
            }
            for todo in todos
        ]
        writer(
            {
                "type": "todo_update",
                "node": "verifier",
                "plan_summary": state.get("plan_summary", ""),
                "todos": todos,
                "verification_commands": state.get("verification_commands", []),
            }
        )
    last_error = "" if passed else _format_verifier_error(reason, recommended, tool_events)

    return {
        "messages": produced_messages,
        "verification_results": _tool_events_to_verification_results(tool_events),
        "verification_checks": checks,
        "verifier_summary": reason,
        "passed": passed,
        "attempts": attempts,
        "last_error": last_error,
        "todos": todos,
        "memory_snapshot": memory,
        "history_summary": memory.get("history_summary_store", {}).get("history_summary", ""),
        "context_next_node": verifier_route({**state, "passed": passed, "attempts": attempts}),
    }


def context_monitor_node(state: CongGraphState) -> dict[str, Any]:
    writer = _get_writer()
    token_limit = get_context_token_limit()
    token_count = estimate_context_tokens(state)
    should_compress = token_count >= token_limit
    next_node = state.get("context_next_node") or "verifier"
    event = {
        "type": "context_monitor",
        "token_count": token_count,
        "token_limit": token_limit,
        "should_compress": should_compress,
        "next_node": next_node,
        "message_count": len(state.get("messages", [])),
    }
    writer(event)
    return {
        "context_token_count": token_count,
        "context_token_limit": token_limit,
        "context_should_compress": should_compress,
        "context_next_node": next_node,
    }


def context_monitor_route(state: CongGraphState) -> str:
    if state.get("context_should_compress"):
        return "context_compressor"
    return state.get("context_next_node") or "verifier"


def context_compressor_node(state: CongGraphState) -> dict[str, Any]:
    writer = _get_writer()
    before_tokens = state.get("context_token_count") or estimate_context_tokens(state)
    before_messages = list(state.get("messages", []))
    memory = build_layered_memory(state, node="context_compressor")
    writer(memory_event(memory, node="context_compressor"))
    compressed = _compress_context_with_model(state)
    summary = _format_compressed_context(compressed, state)
    summary_message = AIMessage(content=summary)
    persist_history_summary(state["runtime"], summary)

    post_state: CongGraphState = {
        **state,
        "messages": [summary_message],
        "context_summary": summary,
        "history_summary": summary,
        "memory_snapshot": build_layered_memory(
            {**state, "context_summary": summary, "history_summary": summary},
            node="context_compressor",
        ),
        "research_notes": _short_text(state.get("research_notes", ""), 1200),
        "agent_handoffs": _trim_handoffs(state.get("agent_handoffs", [])),
        "last_error": _short_text(state.get("last_error", ""), 1600),
        "code_agent_summary": _short_text(state.get("code_agent_summary", ""), 1200),
        "verifier_summary": _short_text(state.get("verifier_summary", ""), 1200),
    }
    after_tokens = estimate_context_tokens(post_state)
    compression_event = {
        "before_tokens": int(before_tokens),
        "after_tokens": int(after_tokens),
        "removed_messages": len(before_messages),
        "summary": _short_text(summary, 1200),
        "next_node": state.get("context_next_node", "verifier"),
    }
    events = list(state.get("compression_events", [])) + [compression_event]
    writer({"type": "context_compression", **compression_event})
    return {
        "messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), summary_message],
        "context_summary": summary,
        "context_token_count": after_tokens,
        "context_should_compress": False,
        "research_notes": post_state.get("research_notes", ""),
        "agent_handoffs": post_state.get("agent_handoffs", []),
        "last_error": post_state.get("last_error", ""),
        "code_agent_summary": post_state.get("code_agent_summary", ""),
        "verifier_summary": post_state.get("verifier_summary", ""),
        "memory_snapshot": post_state.get("memory_snapshot", {}),
        "history_summary": summary,
        "compression_events": events,
    }


def context_compressor_route(state: CongGraphState) -> str:
    return state.get("context_next_node") or "verifier"


def verifier_route(state: CongGraphState) -> str:
    if state.get("passed"):
        return "final"
    if state.get("attempts", 0) >= state.get("max_attempts", 3):
        return "final"
    return "planner"


def final_node(state: CongGraphState) -> dict[str, Any]:
    status = "PASSED" if state.get("passed") else "FAILED"
    checks = "\n".join(
        f"- {check.get('name', 'check')}: {'PASS' if check.get('passed') else 'FAIL'} - {check.get('detail', '')}"
        for check in state.get("verification_checks", [])
    )
    todos = "\n".join(f"- [{todo.get('status', '')}] {todo.get('content', '')}" for todo in state.get("todos", []))
    sources = "\n".join(f"- {source.get('title', '')}: {source.get('url', '')}" for source in state.get("sources", []))
    compression_events = state.get("compression_events", [])
    compression_text = "(none)"
    if compression_events:
        latest = compression_events[-1]
        compression_text = (
            f"{len(compression_events)} compression(s); "
            f"latest {latest.get('before_tokens')} -> {latest.get('after_tokens')} tokens; "
            f"removed {latest.get('removed_messages')} message(s)"
        )
    final_answer = (
        f"LangGraph MultiAgent workflow finished: {status}\n\n"
        f"Plan: {state.get('plan_summary', '')}\n\n"
        f"Todos:\n{todos}\n\n"
        f"Sources:\n{sources or '(none)'}\n\n"
        f"Verifier:\n{state.get('verifier_summary', '')}\n\n"
        f"Checks:\n{checks or '(none)'}\n\n"
        f"Context compression:\n{compression_text}\n\n"
        f"Agent summary:\n{state.get('code_agent_summary') or state.get('last_actor_summary', '')}"
    )
    return {"final_answer": final_answer}


def get_context_token_limit() -> int:
    load_dotenv()
    raw = os.getenv("CONG_CONTEXT_TOKEN_LIMIT", str(DEFAULT_CONTEXT_TOKEN_LIMIT))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_CONTEXT_TOKEN_LIMIT
    return value if value > 0 else DEFAULT_CONTEXT_TOKEN_LIMIT


def estimate_context_tokens(state: CongGraphState) -> int:
    messages = list(state.get("messages", []))
    payload = build_layered_memory(state, node="context_monitor")
    payload_message = HumanMessage(content=json.dumps(payload, ensure_ascii=False, default=str))
    try:
        model = create_model()
        return int(model.get_num_tokens_from_messages(messages + [payload_message]))
    except Exception:
        text = "\n".join(_message_text(message) for message in messages)
        text += "\n" + payload_message.content
        return max(1, len(text) // 4)


def _build_planner_tools(state: CongGraphState, writer) -> list[StructuredTool]:
    # searchAgent / codeAgent 已随领域瘦身删除；客服业务 Skill 将在阶段 4 接入。
    return [
        StructuredTool.from_function(
            name="TodoWriteTool",
            func=lambda todos, acceptance_criteria, verification_commands, plan_summary="": _todo_write_tool(
                state, writer, todos, acceptance_criteria, verification_commands, plan_summary
            ),
            description=(
                "Publish or revise plan state. Args: todos, acceptance_criteria, "
                "verification_commands, optional plan_summary."
            ),
        ),
    ]


def _todo_write_tool(
    state: CongGraphState,
    writer,
    todos: Any,
    acceptance_criteria: Any,
    verification_commands: Any,
    plan_summary: str = "",
) -> dict[str, Any]:
    result = write_todos(todos, acceptance_criteria, verification_commands)
    if result.get("ok"):
        state["plan_summary"] = plan_summary or state.get("plan_summary") or "MultiAgent plan"
        state["todos"] = _todo_items(result["todos"], existing=state.get("todos", []))
        state["acceptance_criteria"] = result["acceptance_criteria"]
        state["verification_commands"] = result["verification_commands"]
        persist_todos(
            state["runtime"],
            state["todos"],
            state["acceptance_criteria"],
            state["verification_commands"],
            state.get("plan_summary", ""),
        )
        writer(
            {
                "type": "plan_snapshot",
                "node": "planner",
                "plan_summary": state.get("plan_summary", ""),
                "todos": state.get("todos", []),
                "verification_commands": state.get("verification_commands", []),
                "acceptance_criteria": state.get("acceptance_criteria", []),
            }
        )
    return {
        **result,
        "plan_summary": state.get("plan_summary", ""),
        "todo_items": state.get("todos", []),
    }


def _execute_planner_tool(state: CongGraphState, writer, call: dict[str, Any]) -> ToolMessage:
    name = call.get("name", "")
    args = call.get("args") or {}
    writer({"type": "tool_call", "node": "planner", "name": name, "args": args})
    tools = {tool.name: tool for tool in _build_planner_tools(state, writer)}
    tool = tools.get(name)
    if tool is None:
        result = {"ok": False, "error": f"unknown tool: {name}"}
    else:
        try:
            result = tool.invoke(args)
        except Exception as exc:
            result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    tool_message = ToolMessage(
        content=json.dumps(result, ensure_ascii=False),
        name=name,
        tool_call_id=call.get("id") or f"{name}-call",
    )
    writer(_tool_result_event(tool_message, node="planner"))
    return tool_message


def _compress_context_with_model(state: CongGraphState) -> dict[str, Any]:
    memory = build_layered_memory(state, node="context_compressor")
    payload = {
        "context_summary": state.get("context_summary", ""),
        "memory": memory,
        "messages": [_message_snapshot(message) for message in state.get("messages", [])],
    }
    messages = [
        SystemMessage(content=CONTEXT_COMPRESSION_PROMPT),
        HumanMessage(content=json.dumps(payload, ensure_ascii=False, default=str)),
    ]
    try:
        response = create_model().invoke(messages)
        parsed = _extract_json(str(response.content))
        if parsed:
            return parsed
    except Exception as exc:
        return _fallback_compression(state, error=f"{type(exc).__name__}: {exc}")
    return _fallback_compression(state, error="compressor model did not return valid JSON")


def _fallback_compression(state: CongGraphState, *, error: str = "") -> dict[str, Any]:
    return {
        "summary": _short_text(
            "\n\n".join(
                [
                    state.get("context_summary", ""),
                    state.get("research_notes", ""),
                    state.get("code_agent_summary", ""),
                    state.get("verifier_summary", ""),
                    state.get("last_error", ""),
                ]
            ),
            2400,
        ),
        "active_goal": state.get("task", ""),
        "completed_work": state.get("code_agent_summary", ""),
        "open_todos": [
            todo.get("content", "")
            for todo in state.get("todos", [])
            if todo.get("status") != "completed"
        ],
        "important_files": _important_files_from_state(state),
        "tool_findings": _short_text(state.get("last_error", ""), 1200),
        "sources": [{"title": source.get("title", ""), "url": source.get("url", "")} for source in state.get("sources", [])],
        "next_steps": state.get("context_next_node", ""),
        "risks": error,
    }


def _format_compressed_context(compressed: dict[str, Any], state: CongGraphState) -> str:
    payload = {
        "type": "cong_context_summary",
        "task": state.get("task", ""),
        "plan_summary": state.get("plan_summary", ""),
        "todos": state.get("todos", []),
        "acceptance_criteria": state.get("acceptance_criteria", []),
        "verification_commands": state.get("verification_commands", []),
        "attempts": state.get("attempts", 0),
        "passed": state.get("passed"),
        "compression": compressed,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def _context_payload(state: CongGraphState) -> dict[str, Any]:
    return build_layered_memory(state, node="graph")


def _message_snapshot(message: Any) -> dict[str, str]:
    return {
        "type": type(message).__name__,
        "name": str(getattr(message, "name", "") or ""),
        "content": _short_text(_message_text(message), 2000),
    }


def _message_text(message: Any) -> str:
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False, default=str)


def _important_files_from_state(state: CongGraphState) -> list[str]:
    files: list[str] = []
    for command in state.get("verification_commands", []):
        files.extend(re.findall(r"[\w./\\-]+\.(?:py|html|css|js|json|md|txt)", command))
    for text in [state.get("code_agent_summary", ""), state.get("last_error", "")]:
        files.extend(re.findall(r"[\w./\\-]+\.(?:py|html|css|js|json|md|txt)", text))
    seen: set[str] = set()
    deduped = []
    for item in files:
        normalized = item.strip("\"'")
        if normalized and normalized not in seen:
            seen.add(normalized)
            deduped.append(normalized)
    return deduped


def _planner_input(state: CongGraphState, memory: dict[str, Any]) -> str:
    parts = [
        f"Task: {state['task']}",
        f"Attempt: {state.get('attempts', 0) + 1}",
    ]
    if state.get("session_context"):
        parts.append("Session context for this multi-turn conversation:\n" + str(state.get("session_context", "")))
    parts.append("Layered memory snapshot:\n" + format_layered_memory_for_prompt(memory))
    return "\n\n".join(parts)


def _verifier_input(state: CongGraphState, memory: dict[str, Any]) -> str:
    parts = [f"Task: {state['task']}"]
    if state.get("session_context"):
        parts.append("Session context for this multi-turn conversation:\n" + str(state.get("session_context", "")))
    parts.append("Layered memory snapshot:\n" + format_layered_memory_for_prompt(memory))
    parts.append("Return only verifier JSON.")
    return "\n\n".join(parts)


def _default_plan(task: str) -> dict[str, Any]:
    return {
        "plan_summary": "Clarify and handle the customer service request.",
        "todos": DEFAULT_TODOS,
        "acceptance_criteria": ["The customer's request is addressed.", "The response is verified before finishing."],
        "verification_commands": [],
    }


def _apply_plan(state: CongGraphState, plan: dict[str, Any]) -> None:
    state["plan_summary"] = str(plan.get("plan_summary", ""))
    state["todos"] = _todo_items([str(item) for item in plan.get("todos", [])], existing=state.get("todos", []))
    state["acceptance_criteria"] = [str(item) for item in plan.get("acceptance_criteria", [])]
    state["verification_commands"] = [str(item) for item in plan.get("verification_commands") or []]


def _todo_items(todos: list[str], *, existing: list[dict[str, Any]] | None = None) -> list[TodoItem]:
    existing_by_content = {todo.get("content", ""): todo for todo in existing or []}
    items: list[TodoItem] = []
    for idx, todo in enumerate(todos, start=1):
        previous = existing_by_content.get(todo, {})
        items.append(
            {
                "id": str(previous.get("id") or f"todo-{idx}"),
                "content": todo,
                "status": str(previous.get("status") or "pending"),
                "note": str(previous.get("note") or ""),
            }
        )
    return items


def _extract_json(text: str) -> dict[str, Any] | None:
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    raw = fenced.group(1) if fenced else text
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        parsed = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _coerce_confidence(value: Any) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, confidence))


def _tool_result_event(tool_message: ToolMessage, *, node: str) -> dict[str, Any]:
    try:
        parsed = json.loads(str(tool_message.content))
    except json.JSONDecodeError:
        parsed = tool_message.content
    return {"type": "tool_result", "node": node, "name": tool_message.name, "result": parsed}


def _tool_events_to_verification_results(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results = []
    for event in events:
        result = event.get("result", {})
        if not isinstance(result, dict):
            continue
        results.append(
            {
                "command": result.get("command") or event.get("name", ""),
                "ok": bool(result.get("ok")),
                "exit_code": result.get("exit_code"),
                "stdout": str(result.get("stdout", "")),
                "stderr": str(result.get("stderr") or result.get("error", "")),
            }
        )
    return results


def _normalize_checks(raw: Any) -> list[VerificationCheck]:
    if not isinstance(raw, list):
        return []
    checks: list[VerificationCheck] = []
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


def _format_verifier_error(reason: str, recommended: str, tool_events: list[dict[str, Any]]) -> str:
    event_text = json.dumps(tool_events[-3:], ensure_ascii=False, default=str)[:1600]
    return (
        f"Verifier failed: {reason}\n"
        f"Recommended next instruction: {recommended}\n"
        f"Recent verifier tool events:\n{event_text}"
    )


def _trim_handoffs(handoffs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    trimmed = []
    for handoff in handoffs[-6:]:
        trimmed.append(
            {
                "from_agent": handoff.get("from_agent", ""),
                "to_agent": handoff.get("to_agent", ""),
                "instruction": _short_text(str(handoff.get("instruction", "")), 500),
                "result": _short_text(str(handoff.get("result", "")), 700),
            }
        )
    return trimmed


def _short_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _last_ai_content(messages: list[Any]) -> str:
    for message in reversed(messages):
        if isinstance(message, ToolMessage):
            continue
        content = getattr(message, "content", "")
        if content:
            return str(content)
    return ""


def _todos_text(todos: list[dict[str, Any]]) -> str:
    return "\n".join(
        f"- {todo.get('id', '')} [{todo.get('status', '')}] {todo.get('content', '')} {todo.get('note', '')}".strip()
        for todo in todos
    )


def _list_text(items: list[str]) -> str:
    return "\n".join(f"- {item}" for item in items)


def _get_writer():
    try:
        return get_stream_writer()
    except RuntimeError:
        return lambda _: None
