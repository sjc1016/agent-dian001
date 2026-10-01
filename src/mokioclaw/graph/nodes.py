from __future__ import annotations

import asyncio
import json
import os
import re
import threading
from typing import Any

from dotenv import load_dotenv
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.config import get_stream_writer
from langgraph.graph.message import REMOVE_ALL_MESSAGES

from mokioclaw.graph.memory import (
    build_layered_memory,
    format_layered_memory_for_prompt,
    memory_event,
    persist_history_summary,
)
from mokioclaw.graph.state import MokioGraphState, TodoItem, VerificationCheck
from mokioclaw.prompts.intent import (
    AGENT_PLACEHOLDER_REPLY,
    CLARIFY_MAX_ROUNDS,
    CLARIFY_PROMPT,
    FALLBACK_TEMPLATES,
    INTENT_CATEGORIES,
    INTENT_ROUTER_PROMPT,
    UNKNOWN_CONFIDENCE_FLOOR,
    UNKNOWN_MAX_STREAK,
)
from mokioclaw.prompts.stage3 import PLANNER_PROMPT, VERIFIER_PROMPT
from mokioclaw.prompts.stage4 import CONTEXT_COMPRESSION_PROMPT
from mokioclaw.providers.openai_provider import create_model
from mokioclaw.rag.workflow import build_rag_subgraph
from mokioclaw.tools.todo_tool import persist_todos, write_todos


DEFAULT_CONTEXT_TOKEN_LIMIT = 400000

DEFAULT_TODOS = [
    "Clarify the customer's request and required slots.",
    "Handle the request via retrieval or business skills.",
    "Reflect on and verify the response before finishing.",
]

def intent_router_node(state: MokioGraphState) -> dict[str, Any]:
    """阶段 2：五类意图识别 + 追问/unknown 计数 + 强制兜底路由。

    类别：rag_query / agent_service / clarify / irrelevant / unknown；
    路由：rag_answer / agent_loop / clarify / fallback（unknown 未达阈值先追问）。
    """
    writer = _get_writer()
    category = "unknown"
    reason = "router fallback: default to unknown"
    confidence = 0.0
    pending_slots: list[str] = []
    try:
        response = create_model().invoke(
            [
                SystemMessage(content=INTENT_ROUTER_PROMPT),
                HumanMessage(content=_router_input(state)),
            ]
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


def intent_route_fn(state: MokioGraphState) -> str:
    return str(state.get("intent_route") or "fallback")


def clarify_node(state: MokioGraphState) -> dict[str, Any]:
    """P2-4：按 pending_slots 生成追问话术，clarify_count += 1。"""
    writer = _get_writer()
    pending_slots = [str(slot) for slot in (state.get("pending_slots") or []) if str(slot).strip()]
    question = ""
    try:
        response = create_model().invoke(
            [
                SystemMessage(content=CLARIFY_PROMPT),
                HumanMessage(content=_clarify_input(state, pending_slots)),
            ]
        )
        question = str(getattr(response, "content", "") or "").strip()
    except Exception as exc:
        writer({"type": "clarify_model_error", "error": f"{type(exc).__name__}: {exc}"})
    if not question:
        question = _default_clarify_question(pending_slots)
    clarify_count = int(state.get("clarify_count", 0) or 0) + 1
    event = {
        "type": "clarify_question",
        "question": question,
        "pending_slots": pending_slots,
        "clarify_count": clarify_count,
        "category": state.get("intent_category", "clarify"),
    }
    writer(event)
    return {
        "clarify_count": clarify_count,
        "clarify_question": question,
        "chat_response": question,
        "final_answer": question,
    }


def fallback_node(state: MokioGraphState) -> dict[str, Any]:
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


def rag_answer_node(state: MokioGraphState) -> dict[str, Any]:
    """P3-21：RAG 检索子图挂载点（替换阶段 2 占位）。

    主图与子图状态解耦：问句进、答案+来源出。子图节点全部是 async，
    主图当前由同步 stream 驱动（工作线程内无运行中的事件循环），
    因此在独立事件循环里用 astream 驱动编译后的子图，并把子图 custom
    事件（两路召回/融合分/精排分/证据门...）实时转发到主图流，接入 trace。
    若主图本身被 astream 驱动，则在独立线程里起循环，避免嵌套事件循环。
    """
    writer = _get_writer()
    task = str(state.get("task") or "")
    sub_input = {
        "query": task,
        "original_query": task,
        "rag_attempts": 0,
        "session_context": str(state.get("session_context") or ""),
    }
    writer({"type": "rag_start", "query": task})

    async def _drive_subgraph() -> dict[str, Any]:
        final_state: dict[str, Any] = {}
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
                        final_state.update(update)
        writer({"type": "rag_trace", "node_sequence": node_sequence})
        return final_state

    try:
        result = _run_async(_drive_subgraph)
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


def _run_async(factory):
    """在同步主图节点里运行协程：无运行循环时 asyncio.run，否则开独立线程。"""
    try:
        asyncio.get_running_loop()
        running_loop = True
    except RuntimeError:
        running_loop = False

    if not running_loop:
        return asyncio.run(factory())

    box: dict[str, Any] = {}

    def _worker() -> None:
        try:
            box["result"] = asyncio.run(factory())
        except BaseException as exc:  # noqa: BLE001
            box["error"] = exc

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]
    return box.get("result")


def agent_loop_node(state: MokioGraphState) -> dict[str, Any]:
    """P2-9：Agent 占位节点（阶段 4 替换为业务推理子图）。"""
    writer = _get_writer()
    writer(
        {
            "type": "agent_placeholder",
            "task": state.get("task", ""),
            "pending_slots": [str(slot) for slot in (state.get("pending_slots") or [])],
        }
    )
    return {"chat_response": AGENT_PLACEHOLDER_REPLY, "final_answer": AGENT_PLACEHOLDER_REPLY}


def _clarify_input(state: MokioGraphState, pending_slots: list[str]) -> str:
    parts = [f"用户输入：{state.get('task', '')}"]
    if pending_slots:
        parts.append("待补槽位：" + "、".join(pending_slots[:3]))
    if state.get("session_context"):
        parts.append("会话上下文：\n" + str(state.get("session_context", "")))
    return "\n\n".join(parts)


def _default_clarify_question(pending_slots: list[str]) -> str:
    if pending_slots:
        return "为了更好地为您办理，请补充：" + "、".join(pending_slots[:3]) + "。"
    return (
        "请问您想咨询或办理哪项业务呢？例如：查询话费余额、了解或变更套餐、"
        "宽带/手机故障报修、资费规则咨询。"
    )


def planner_node(state: MokioGraphState) -> dict[str, Any]:
    writer = _get_writer()
    working_state: MokioGraphState = {**state}
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


def verifier_node(state: MokioGraphState) -> dict[str, Any]:
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


def context_monitor_node(state: MokioGraphState) -> dict[str, Any]:
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


def context_monitor_route(state: MokioGraphState) -> str:
    if state.get("context_should_compress"):
        return "context_compressor"
    return state.get("context_next_node") or "verifier"


def context_compressor_node(state: MokioGraphState) -> dict[str, Any]:
    writer = _get_writer()
    before_tokens = state.get("context_token_count") or estimate_context_tokens(state)
    before_messages = list(state.get("messages", []))
    memory = build_layered_memory(state, node="context_compressor")
    writer(memory_event(memory, node="context_compressor"))
    compressed = _compress_context_with_model(state)
    summary = _format_compressed_context(compressed, state)
    summary_message = AIMessage(content=summary)
    persist_history_summary(state["runtime"], summary)

    post_state: MokioGraphState = {
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


def context_compressor_route(state: MokioGraphState) -> str:
    return state.get("context_next_node") or "verifier"


def verifier_route(state: MokioGraphState) -> str:
    if state.get("passed"):
        return "final"
    if state.get("attempts", 0) >= state.get("max_attempts", 3):
        return "final"
    return "planner"


def final_node(state: MokioGraphState) -> dict[str, Any]:
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
    raw = os.getenv("MOKIO_CONTEXT_TOKEN_LIMIT", str(DEFAULT_CONTEXT_TOKEN_LIMIT))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_CONTEXT_TOKEN_LIMIT
    return value if value > 0 else DEFAULT_CONTEXT_TOKEN_LIMIT


def estimate_context_tokens(state: MokioGraphState) -> int:
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


def _build_planner_tools(state: MokioGraphState, writer) -> list[StructuredTool]:
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
    state: MokioGraphState,
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


def _execute_planner_tool(state: MokioGraphState, writer, call: dict[str, Any]) -> ToolMessage:
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


def _compress_context_with_model(state: MokioGraphState) -> dict[str, Any]:
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


def _fallback_compression(state: MokioGraphState, *, error: str = "") -> dict[str, Any]:
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


def _format_compressed_context(compressed: dict[str, Any], state: MokioGraphState) -> str:
    payload = {
        "type": "mokio_context_summary",
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


def _context_payload(state: MokioGraphState) -> dict[str, Any]:
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


def _important_files_from_state(state: MokioGraphState) -> list[str]:
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


def _planner_input(state: MokioGraphState, memory: dict[str, Any]) -> str:
    parts = [
        f"Task: {state['task']}",
        f"Attempt: {state.get('attempts', 0) + 1}",
    ]
    if state.get("session_context"):
        parts.append("Session context for this multi-turn conversation:\n" + str(state.get("session_context", "")))
    parts.append("Layered memory snapshot:\n" + format_layered_memory_for_prompt(memory))
    return "\n\n".join(parts)


def _verifier_input(state: MokioGraphState, memory: dict[str, Any]) -> str:
    parts = [f"Task: {state['task']}"]
    if state.get("session_context"):
        parts.append("Session context for this multi-turn conversation:\n" + str(state.get("session_context", "")))
    parts.append("Layered memory snapshot:\n" + format_layered_memory_for_prompt(memory))
    parts.append("Return only verifier JSON.")
    return "\n\n".join(parts)


def _router_input(state: MokioGraphState) -> str:
    parts = [f"User input:\n{state.get('task', '')}"]
    if state.get("session_context"):
        parts.append("Session context:\n" + str(state.get("session_context", "")))
    return "\n\n".join(parts)


def _default_plan(task: str) -> dict[str, Any]:
    return {
        "plan_summary": "Clarify and handle the customer service request.",
        "todos": DEFAULT_TODOS,
        "acceptance_criteria": ["The customer's request is addressed.", "The response is verified before finishing."],
        "verification_commands": [],
    }


def _apply_plan(state: MokioGraphState, plan: dict[str, Any]) -> None:
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
