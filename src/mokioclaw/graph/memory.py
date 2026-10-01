from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from mokioclaw.core.state import RuntimeState

HISTORY_SUMMARY_FILE = "HISTORY_SUMMARY.md"

_TEXT_ENCODINGS = ("utf-8", "utf-8-sig", "gbk")

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
    content = f"# MokioClaw History Summary\n\n_Updated: {timestamp}_\n\n{summary.strip()}\n"
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
