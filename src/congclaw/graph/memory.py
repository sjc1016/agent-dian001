from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

from congclaw.core.state import RuntimeState

HISTORY_SUMMARY_FILE = "HISTORY_SUMMARY.md"

_TEXT_ENCODINGS = ("utf-8", "utf-8-sig", "gbk")

# ---------------------------------------------------------------------------
# 阶段 5：客服域分层记忆
# 四层 = 系统提示词层（system）/ 短期会话窗口（working_memory）
#       / 长期用户摘要（long_term_profile）/ 检索证据（evidence）
# ---------------------------------------------------------------------------

SHORT_WINDOW_TURNS = 6  # 短期记忆滑动窗口：最近 N 个用户回合（user/assistant 成对）
WINDOW_LINE_CHARS = 300

CUSTOMER_SYSTEM_LAYER = {
    "scope": "telecom_customer_service",
    "storage": "internal",
    "rules": [
        "仅服务电信客服四类场景：话费余额查询、套餐咨询与办理、故障报修、资费规则咨询。",
        "运行时按四层组装提示词：系统提示词 → 记忆（短期窗口+长期摘要）→ 检索证据 → 用户问题。",
        "短期会话窗口仅供指代消解与上下文理解；长期用户摘要仅供理解用户背景，不得当作本轮明确诉求。",
        "不得向用户暴露记忆/证据的原始 JSON 与内部存储结构。",
    ],
}

RULES_LAYER = {
    "scope": "telecom_customer_service",
    "storage": "internal",
    "rules": [
        "Serve only telecom customer-service scenarios: balance, packages, fault reports, and service handling.",
        "Assemble layered memory at runtime; do not expose memory write tools to the model.",
        "Treat the short conversation window, long-term user summary, and retrieved evidence as separate layers.",
    ],
}

MAX_TEXT_CHARS = {
    "research_notes": 1600,
    "agent_handoff_instruction": 500,
    "agent_handoff_result": 700,
    "code_agent_summary": 1000,
    "verifier_summary": 1000,
    "last_error": 1400,
    "context_summary": 1600,
    "session_context": 1800,
    "history_summary": 2200,
}


def _read_text_lossy(path: Path) -> str:
    """读取文本文件，依次尝试常见中文编码，全部失败时退化为替换字符读取。"""
    last_error: UnicodeDecodeError | None = None
    for encoding in _TEXT_ENCODINGS:
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError as exc:
            last_error = exc
    if last_error is not None:
        return path.read_text(encoding="utf-8", errors="replace")
    return path.read_text(encoding="utf-8")


def build_layered_memory(state: dict[str, Any], *, node: str = "graph") -> dict[str, Any]:
    runtime = state["runtime"]
    history = read_history_summary(runtime)
    sources = [
        {
            "title": source.get("title", ""),
            "url": source.get("url", ""),
        }
        for source in state.get("sources", [])
    ]
    working_memory = {
        "node": node,
        "task": state.get("task", ""),
        "session_id": state.get("session_id", ""),
        "session_turn": state.get("session_turn", 0),
        "session_context": _short_text(state.get("session_context", ""), MAX_TEXT_CHARS["session_context"]),
        "plan_summary": state.get("plan_summary", ""),
        "todos": state.get("todos", []),
        "acceptance_criteria": state.get("acceptance_criteria", []),
        "verification_commands": state.get("verification_commands", []),
        "research_notes": _short_text(state.get("research_notes", ""), MAX_TEXT_CHARS["research_notes"]),
        "sources": sources,
        "agent_handoffs": _trim_handoffs(state.get("agent_handoffs", [])),
        "code_agent_summary": _short_text(state.get("code_agent_summary", ""), MAX_TEXT_CHARS["code_agent_summary"]),
        "verifier_summary": _short_text(state.get("verifier_summary", ""), MAX_TEXT_CHARS["verifier_summary"]),
        "verification_checks": state.get("verification_checks", []),
        "last_error": _short_text(state.get("last_error", ""), MAX_TEXT_CHARS["last_error"]),
        "attempts": state.get("attempts", 0),
        "max_attempts": state.get("max_attempts", 3),
        "context_next_node": state.get("context_next_node", ""),
    }
    history_summary = state.get("history_summary") or history.get("content", "")
    history_summary_store = {
        "history_path": HISTORY_SUMMARY_FILE,
        "history_exists": history.get("exists", False),
        "history_summary": _short_text(history_summary, MAX_TEXT_CHARS["history_summary"]),
        "context_summary": _short_text(state.get("context_summary", ""), MAX_TEXT_CHARS["context_summary"]),
        "compression_events": state.get("compression_events", [])[-3:],
    }
    return {
        "rules": dict(RULES_LAYER),
        "working_memory": working_memory,
        "history_summary_store": history_summary_store,
    }


def format_layered_memory_for_prompt(memory: dict[str, Any]) -> str:
    return json.dumps(memory, ensure_ascii=False, indent=2, default=str)


def memory_event(memory: dict[str, Any], *, node: str) -> dict[str, Any]:
    working = memory.get("working_memory", {})
    history = memory.get("history_summary_store", {})
    return {
        "type": "memory_snapshot",
        "node": node,
        "rules_count": len(memory.get("rules", {}).get("rules", [])),
        "todo_count": len(working.get("todos", [])),
        "source_count": len(working.get("sources", [])),
        "handoff_count": len(working.get("agent_handoffs", [])),
        "history_exists": bool(history.get("history_exists")),
        "history_path": history.get("history_path", HISTORY_SUMMARY_FILE),
        "layers": {
            "rules": _event_layer_summary(memory.get("rules", {})),
            "working_memory": _event_layer_summary(working),
            "history_summary_store": _event_layer_summary(history),
        },
    }


def read_history_summary(state: RuntimeState) -> dict[str, Any]:
    path = state.assert_workspace_path(state.workspace / HISTORY_SUMMARY_FILE)
    if not path.exists():
        return {"ok": True, "path": HISTORY_SUMMARY_FILE, "content": "", "exists": False}
    content = _read_text_lossy(path)
    state.record_read(path, complete=True)
    return {"ok": True, "path": HISTORY_SUMMARY_FILE, "content": content, "exists": True}


def persist_history_summary(state: RuntimeState, summary: str) -> dict[str, Any]:
    path = state.assert_workspace_path(state.workspace / HISTORY_SUMMARY_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    content = f"# CongClaw History Summary\n\n_Updated: {timestamp}_\n\n{summary.strip()}\n"
    path.write_text(content, encoding="utf-8")
    state.record_read(path, complete=True)
    return {"ok": True, "path": HISTORY_SUMMARY_FILE, "lines": len(content.splitlines())}


def _event_layer_summary(layer: dict[str, Any]) -> str:
    if not layer:
        return "(empty)"
    text = json.dumps(layer, ensure_ascii=False, default=str)
    return _short_text(text, 420)


def _trim_handoffs(handoffs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    trimmed = []
    for handoff in handoffs[-6:]:
        trimmed.append(
            {
                "from_agent": handoff.get("from_agent", ""),
                "to_agent": handoff.get("to_agent", ""),
                "instruction": _short_text(
                    str(handoff.get("instruction", "")),
                    MAX_TEXT_CHARS["agent_handoff_instruction"],
                ),
                "result": _short_text(str(handoff.get("result", "")), MAX_TEXT_CHARS["agent_handoff_result"]),
            }
        )
    return trimmed


def _short_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


# ===========================================================================
# 阶段 5：客服域四层记忆（P5-4 / P5-5 / P5-9）
#
# 旧 ``build_layered_memory`` 仅服务遗留 complex 工作流（planner/verifier）；
# 客服主图（query_rewrite / intent_router / clarify / RAG / Agent）统一使用
# 以下构建与组装入口：
#
#   build_customer_memory()      组装四层结构化记忆
#   render_memory_sections()     渲染记忆层（短期窗口 + 长期摘要）文本
#   render_evidence_section()    渲染检索证据层文本
#   compose_layered_content()    记忆 → 证据 → 问题 的统一段落拼装
#   assemble_layered_messages()  系统提示词 + 四层 HumanMessage 消息对
# ===========================================================================


def build_customer_memory(
    state: dict[str, Any], *, node: str = "graph", evidence: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """构建客服域四层记忆：system / working_memory / long_term_profile / evidence。

    - working_memory：短期会话窗口（最近 N 轮）+ 当前意图 + 待确认槽位（P5-4）；
    - long_term_profile：跨会话长期用户摘要（历史主题、在办工单、偏好套餐）（P5-5）；
    - evidence：检索证据层（RAG 子图内由父分片证据注入，主图通常为空）。
    """
    profile = _normalize_profile(state.get("user_profile"))
    window = _short_term_window(state)
    current_question = str(state.get("rewritten_task") or state.get("task") or "")
    working_memory = {
        "node": node,
        "session_id": str(state.get("session_id") or ""),
        "session_turn": int(state.get("session_turn", 0) or 0),
        "phone": str(state.get("phone") or ""),
        "current_intent": str(state.get("intent_category") or ""),
        "intent_route": str(state.get("intent_route") or ""),
        "pending_slots": [str(slot) for slot in (state.get("pending_slots") or []) if str(slot).strip()],
        "clarify_count": int(state.get("clarify_count", 0) or 0),
        "current_question": current_question,
        "short_term_window": window,
    }
    long_term_profile = {
        "exists": bool(profile.get("exists")),
        "phone": profile.get("phone", ""),
        "summary": profile.get("summary", ""),
        "topics": profile.get("topics", []),
        "open_tickets": profile.get("open_tickets", []),
        "preferred_package": profile.get("preferred_package", ""),
    }
    return {
        "system": dict(CUSTOMER_SYSTEM_LAYER),
        "working_memory": working_memory,
        "long_term_profile": long_term_profile,
        "evidence": _normalize_evidence(evidence or []),
    }


def render_memory_sections(memory: dict[str, Any]) -> str:
    """渲染记忆层（短期会话窗口 + 长期用户摘要）为提示词段落；全空时返回空串。"""
    sections: list[str] = []
    working = memory.get("working_memory", {}) if isinstance(memory, dict) else {}
    window = working.get("short_term_window") or []
    intent_lines: list[str] = []
    if working.get("current_intent"):
        intent_lines.append(f"当前意图：{working.get('current_intent')}")
    pending = working.get("pending_slots") or []
    if pending:
        intent_lines.append("待确认槽位：" + "、".join(str(s) for s in pending[:4]))
    if window or intent_lines:
        lines = []
        for item in window:
            speaker = "用户" if item.get("role") == "user" else "客服"
            lines.append(f"{speaker}：{item.get('content', '')}")
        lines.extend(intent_lines)
        sections.append(
            "【会话记忆（当前会话短期窗口，仅供理解指代与省略，勿向用户复述原始记录）】\n"
            + "\n".join(lines)
        )

    profile = memory.get("long_term_profile", {}) if isinstance(memory, dict) else {}
    profile_lines: list[str] = []
    if profile.get("summary"):
        profile_lines.append(str(profile["summary"]))
    topics = profile.get("topics") or []
    if topics:
        profile_lines.append("历史咨询主题：" + "、".join(str(t) for t in topics[:8]))
    tickets = profile.get("open_tickets") or []
    if tickets:
        ticket_text = "、".join(
            f"{t.get('fault_type') or '故障'}工单 {t.get('ticket_id')}（{t.get('status') or '处理中'}）"
            for t in tickets[:5]
            if isinstance(t, dict)
        )
        if ticket_text:
            profile_lines.append("在办工单：" + ticket_text)
    if profile.get("preferred_package"):
        profile_lines.append(f"偏好/关注套餐：{profile['preferred_package']}")
    if profile_lines:
        sections.append(
            "【长期用户摘要（跨会话记忆，仅供理解用户背景，勿当作本轮明确诉求）】\n"
            + "\n".join(profile_lines)
        )
    return "\n\n".join(sections)


def render_evidence_section(evidence: list[dict[str, Any]] | None) -> str:
    """渲染检索证据层；无证据返回空串。兼容 RAG 父分片证据与 sources 两种结构。"""
    items = _normalize_evidence(evidence or [])
    if not items:
        return ""
    lines = []
    for item in items:
        source = item.get("doc_source") or item.get("title") or "知识库"
        lines.append(f"[{item['source_no']}] 来源：{source}\n{item['text']}")
    return "【检索到的知识库证据（仅可依据以下内容作答）】\n" + "\n\n".join(lines)


def compose_layered_content(
    *,
    question: str,
    memory: dict[str, Any] | None = None,
    memory_text: str = "",
    evidence: list[dict[str, Any]] | None = None,
    extra_sections: list[str] | None = None,
) -> str:
    """统一四层段落拼装：记忆 → 证据 → 附加段 → 用户问题（P5-9 各节点共用）。"""
    sections: list[str] = []
    memory_content = (memory_text or "").strip() or render_memory_sections(memory or {})
    if memory_content:
        sections.append(memory_content)
    evidence_text = render_evidence_section(evidence)
    if evidence_text:
        sections.append(evidence_text)
    for extra in extra_sections or []:
        text = str(extra or "").strip()
        if text:
            sections.append(text)
    sections.append("【用户问题】\n" + str(question or ""))
    return "\n\n".join(sections)


def assemble_layered_messages(
    system_prompt: str,
    state: dict[str, Any] | None,
    *,
    question: str,
    evidence: list[dict[str, Any]] | None = None,
    node: str = "graph",
    extra_sections: list[str] | None = None,
) -> list[BaseMessage]:
    """统一四层提示词组装入口：返回 [SystemMessage, HumanMessage]。

    系统提示词 → 记忆（短期窗口+长期摘要）→ 检索证据 → 用户问题。
    """
    memory = build_customer_memory(state or {}, node=node, evidence=evidence)
    content = compose_layered_content(
        question=question,
        memory=memory,
        evidence=evidence,
        extra_sections=extra_sections,
    )
    return [SystemMessage(content=system_prompt), HumanMessage(content=content)]


def customer_memory_event(memory: dict[str, Any], *, node: str) -> dict[str, Any]:
    """四层记忆快照事件（接入 trace，供阶段 6 评测消费）。"""
    working = memory.get("working_memory", {})
    profile = memory.get("long_term_profile", {})
    return {
        "type": "customer_memory_snapshot",
        "node": node,
        "window_turns": len(working.get("short_term_window", [])),
        "current_intent": working.get("current_intent", ""),
        "pending_slots": working.get("pending_slots", []),
        "profile_exists": bool(profile.get("exists")),
        "profile_topics": profile.get("topics", []),
        "evidence_count": len(memory.get("evidence", [])),
    }


def _short_term_window(state: dict[str, Any]) -> list[dict[str, str]]:
    """提取短期会话滑动窗口。

    - ``recent_turns`` 为显式列表（含空列表，即首轮）时直接使用；
    - 仅在调用方未提供该键时，才从 session_context JSON 兜底解析（旧链路兼容）。
    """
    turns = state.get("recent_turns")
    if not isinstance(turns, list):
        turns = _turns_from_session_context(str(state.get("session_context") or ""))
    window = turns[-SHORT_WINDOW_TURNS * 2 :]
    result: list[dict[str, str]] = []
    for turn in window:
        if not isinstance(turn, dict):
            continue
        role = str(turn.get("role") or "")
        if role not in ("user", "assistant"):
            continue
        content = str(turn.get("content") or turn.get("summary") or "")
        content = _short_text(content.strip(), WINDOW_LINE_CHARS)
        if content:
            result.append({"role": role, "content": content})
    return result


def _turns_from_session_context(session_context: str) -> list[dict[str, Any]]:
    if not session_context:
        return []
    try:
        payload = json.loads(session_context)
    except (json.JSONDecodeError, TypeError):
        return []
    turns = payload.get("recent_turns") if isinstance(payload, dict) else None
    return turns if isinstance(turns, list) else []


def _normalize_profile(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {"exists": False, "phone": "", "summary": "", "topics": [], "open_tickets": [], "preferred_package": ""}
    topics = raw.get("topics") if isinstance(raw.get("topics"), list) else []
    tickets = raw.get("open_tickets") if isinstance(raw.get("open_tickets"), list) else []
    exists = bool(
        raw.get("summary")
        or topics
        or tickets
        or raw.get("preferred_package")
        or raw.get("phone")
    )
    return {
        "exists": exists,
        "phone": str(raw.get("phone") or ""),
        "summary": str(raw.get("summary") or ""),
        "topics": topics,
        "open_tickets": tickets,
        "preferred_package": str(raw.get("preferred_package") or ""),
    }


def _normalize_evidence(raw: list[Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for index, entry in enumerate(raw, start=1):
        if not isinstance(entry, dict):
            continue
        text = str(entry.get("text") or entry.get("content") or "").strip()
        if not text:
            continue
        try:
            source_no = int(entry.get("source_no") or index)
        except (TypeError, ValueError):
            source_no = index
        items.append(
            {
                "source_no": source_no,
                "doc_source": str(entry.get("doc_source") or entry.get("title") or ""),
                "text": text,
            }
        )
    return items
