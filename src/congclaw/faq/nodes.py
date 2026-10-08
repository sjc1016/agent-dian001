"""阶段 7：常见问答沉淀子图节点。

四步顺序执行（任一步不通过即短路结束，不产生副作用）：

1. :func:`rule_gate_node`：二次规则门控（隐私、噪音、审批轮问句回溯）；
2. :func:`match_existing_node`：按问句向量召回相近条目，作为 LLM 判重依据；
3. :func:`extract_node`：一次 LLM 调用完成「判定 + 抽取 + create/merge/skip 决策」；
4. :func:`persist_node`：落库（新建或合并递增 merge_count），默认 draft 待审。

设计要点：整条链路对异常完全降级——规则不通过、模型不可用、JSON 非法、
数据库写入失败，都只写 trace 事件并结束，绝不打断刚完成的客服回合。
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.config import get_stream_writer

from congclaw.faq.gate import evaluate_sediment_gate, resolve_sediment_question
from congclaw.faq.state import FaqSedimentState
from congclaw.faq.store import aget_faq, asearch_similar, aupsert_faq
from congclaw.prompts.faq import FAQ_SEDIMENT_PROMPT
from congclaw.providers.openai_provider import create_model

SIMILAR_TOP_K = 5
MAX_ANSWER_CHARS = 1200
MAX_TOOL_PREVIEW_CHARS = 400
MAX_SOURCE_ITEMS = 5
VALID_ACTIONS = ("create", "merge", "skip")
# 抽取超时：本节点位于回合末尾，答案已产出，模型挂起会拖住整轮落库
EXTRACT_TIMEOUT_SECONDS = 30.0


async def rule_gate_node(state: FaqSedimentState) -> dict[str, Any]:
    """规则门控：不满足沉淀条件则短路，避免无谓的向量召回与 LLM 调用。"""
    writer = _get_writer()
    passed, reason = evaluate_sediment_gate(state)
    question = resolve_sediment_question(state)
    writer(
        {
            "type": "faq_gate",
            "passed": passed,
            "reason": reason,
            "question": question[:120],
        }
    )
    return {"gate_passed": passed, "gate_reason": reason, "question": question}


def gate_route(state: FaqSedimentState) -> str:
    """门控条件边：通过则继续判重召回，否则直接结束。"""
    return "match_existing" if state.get("gate_passed") else "end"


async def match_existing_node(state: FaqSedimentState) -> dict[str, Any]:
    """判重召回：取相近条目供 LLM 判断 create / merge / skip。

    召回失败（模型缺失、库异常）不阻断沉淀，仅退化为「无候选」，由 LLM 独立判定。
    """
    writer = _get_writer()
    question = str(state.get("question") or "")
    candidates: list[dict[str, Any]] = []
    error = ""
    try:
        candidates = await asearch_similar(question, top_k=SIMILAR_TOP_K)
    except Exception as exc:  # noqa: BLE001 —— 召回失败不影响沉淀主流程
        error = f"{type(exc).__name__}: {exc}"
    writer(
        {
            "type": "faq_match",
            "question": question[:120],
            "candidate_count": len(candidates),
            "top_similarity": float(candidates[0]["similarity"]) if candidates else 0.0,
            "error": error,
        }
    )
    return {"candidates": candidates}


async def extract_node(state: FaqSedimentState) -> dict[str, Any]:
    """LLM 抽取：判定是否沉淀 + 抽取结构化条目 + 决定与已有条目的关系。"""
    writer = _get_writer()
    payload = _build_sediment_payload(state)
    parsed: dict[str, Any] | None = None
    error = ""
    try:
        response = await asyncio.wait_for(
            create_model().ainvoke(
                [
                    SystemMessage(content=FAQ_SEDIMENT_PROMPT),
                    HumanMessage(content=payload),
                ]
            ),
            timeout=EXTRACT_TIMEOUT_SECONDS,
        )
        parsed = _extract_json(str(getattr(response, "content", "") or ""))
    except Exception as exc:  # noqa: BLE001 —— 模型不可用/超时都不阻断会话，本轮不沉淀
        error = (
            f"TimeoutError: extract exceeded {EXTRACT_TIMEOUT_SECONDS:.0f}s"
            if isinstance(exc, asyncio.TimeoutError)
            else f"{type(exc).__name__}: {exc}"
        )

    if parsed is None:
        reason = error or "invalid_json"
        writer({"type": "faq_sediment_error", "stage": "extract", "error": reason})
        return {
            "should_sediment": False,
            "action": "skip",
            "merge_into": "",
            "entry": {},
            "reason": reason,
            "error": reason,
        }

    should_sediment = bool(parsed.get("should_sediment"))
    action = str(parsed.get("action") or "").strip().lower()
    if action not in VALID_ACTIONS:
        action = "create" if should_sediment else "skip"
    if not should_sediment:
        action = "skip"
    entry = parsed.get("entry") if isinstance(parsed.get("entry"), dict) else {}
    reason = str(parsed.get("reason") or "")
    writer(
        {
            "type": "faq_sediment_decision",
            "should_sediment": should_sediment,
            "action": action,
            "reason": reason[:200],
            "canonical_question": str(entry.get("canonical_question") or "")[:120],
            "category": str(entry.get("category") or ""),
            "confidence": entry.get("confidence"),
        }
    )
    return {
        "should_sediment": should_sediment,
        "action": action,
        "merge_into": str(parsed.get("merge_into") or "").strip(),
        "entry": entry,
        "reason": reason,
        "error": "",
    }


async def persist_node(state: FaqSedimentState) -> dict[str, Any]:
    """落库：新建条目或合并进已有条目（``merge_count`` 递增），默认 draft 待审。"""
    writer = _get_writer()
    action = str(state.get("action") or "skip")
    if action == "skip":
        writer({"type": "faq_sediment_skipped", "reason": str(state.get("reason") or "")[:200]})
        return {"persisted": False}

    entry = dict(state.get("entry") or {})
    if not str(entry.get("canonical_question") or "").strip() or not str(
        entry.get("solution") or ""
    ).strip():
        writer({"type": "faq_sediment_error", "stage": "persist", "error": "empty_entry"})
        return {"persisted": False, "error": "empty_entry"}

    entry["source_route"] = str(state.get("route") or "")
    entry["source_session_id"] = str(state.get("session_id") or "")
    entry["source_turn_index"] = int(state.get("session_turn", 0) or 0)
    entry["sources"] = _clean_sources(state.get("sources"))
    entry["status"] = "draft"

    merge_into = str(state.get("merge_into") or "")
    if action == "merge" and merge_into:
        merged = await _merge_into_existing(entry, merge_into, state)
        if merged is not None:
            entry = merged
        else:
            # 目标条目不存在（并发删除/模型幻觉编号）：降级为新建，避免本轮沉淀丢失
            action = "create"

    try:
        saved = await aupsert_faq(entry)
    except Exception as exc:  # noqa: BLE001 —— 落库失败不回滚已完成的客服回合
        message = f"{type(exc).__name__}: {exc}"
        writer({"type": "faq_sediment_error", "stage": "persist", "error": message})
        return {"persisted": False, "error": message}

    writer(
        {
            "type": "faq_sediment_saved",
            "faq_id": saved["faq_id"],
            "action": action,
            "status": saved["status"],
            "category": saved["category"],
            "merge_count": saved["merge_count"],
            "canonical_question": saved["canonical_question"][:120],
        }
    )
    return {"persisted": True, "faq_id": saved["faq_id"], "action": action}


async def _merge_into_existing(
    entry: dict[str, Any], merge_into: str, state: FaqSedimentState
) -> dict[str, Any] | None:
    """把抽取结果并入已有条目：保留原审核状态与来源，只递增常见度计数。"""
    existing = await aget_faq(merge_into)
    if existing is None:
        return None
    entry["faq_id"] = merge_into
    entry["merge_count"] = int(existing.get("merge_count", 1) or 1) + 1
    entry["hit_count"] = int(existing.get("hit_count", 0) or 0)
    entry["created_at"] = existing.get("created_at", "")
    # 已审核通过的条目保持其状态，避免合并把已发布内容打回草稿
    entry["status"] = str(existing.get("status") or "draft")
    entry["source_route"] = str(existing.get("source_route") or state.get("route") or "")
    if not entry.get("question_variants"):
        entry["question_variants"] = list(existing.get("question_variants") or [])
    return entry


def _build_sediment_payload(state: FaqSedimentState) -> str:
    """组装 LLM 输入：本轮完整问答过程 + 已召回的相近条目。"""
    payload = {
        "本轮用户原问": str(state.get("task") or ""),
        "重写后问题": str(state.get("rewritten_task") or ""),
        "沉淀用问句": str(state.get("question") or ""),
        "意图": {
            "category": str(state.get("category") or ""),
            "route": str(state.get("route") or ""),
            "confidence": float(state.get("confidence", 0.0) or 0.0),
        },
        "业务工具调用": _tool_summaries(state.get("tool_traces")),
        "最终答复": _clip(str(state.get("final_answer") or ""), MAX_ANSWER_CHARS),
        "RAG来源": _source_summaries(state.get("sources")),
        "已有相近FAQ": [
            {
                "id": str(item.get("faq_id") or ""),
                "canonical_question": str(item.get("canonical_question") or ""),
                "solution": str(item.get("solution") or ""),
                "merge_count": int(item.get("merge_count", 1) or 1),
                "similarity": float(item.get("similarity", 0.0) or 0.0),
            }
            for item in (state.get("candidates") or [])
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def _tool_summaries(traces: Any) -> list[dict[str, Any]]:
    if not isinstance(traces, list):
        return []
    summaries = []
    for event in traces:
        if not isinstance(event, dict) or event.get("type") != "skill_result":
            continue
        summaries.append(
            {
                "name": str(event.get("name") or ""),
                "ok": bool(event.get("ok")),
                "preview": _clip(str(event.get("preview") or ""), MAX_TOOL_PREVIEW_CHARS),
            }
        )
    return summaries


def _source_summaries(sources: Any) -> list[dict[str, Any]]:
    if not isinstance(sources, list):
        return []
    summaries = []
    for index, item in enumerate(sources[:MAX_SOURCE_ITEMS], start=1):
        if not isinstance(item, dict):
            continue
        summaries.append(
            {
                "no": index,
                "title": str(item.get("title") or ""),
                "score": float(item.get("score", 0.0) or 0.0),
            }
        )
    return summaries


def _clean_sources(sources: Any) -> list[dict[str, Any]]:
    if not isinstance(sources, list):
        return []
    cleaned = []
    for item in sources[:MAX_SOURCE_ITEMS]:
        if not isinstance(item, dict):
            continue
        cleaned.append(
            {
                "title": str(item.get("title") or ""),
                "url": str(item.get("url") or ""),
                "score": float(item.get("score", 0.0) or 0.0),
            }
        )
    return cleaned


def _extract_json(text: str) -> dict[str, Any] | None:
    """从模型输出中提取 JSON 对象；容忍 ```json 围栏与前后解释文字。"""
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


def _clip(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def _get_writer():
    try:
        return get_stream_writer()
    except RuntimeError:
        return lambda _: None
