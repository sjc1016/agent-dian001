"""阶段 7：沉淀门控规则。

主图的条件边选择器 ``sediment_route`` 与子图首节点 ``rule_gate`` 共用本模块，
保证「哪些回合值得沉淀」只有一处判定口径：条件不满足时根本不进入子图，
既不消耗 LLM 调用，也不会把兜底/无结果/个人事实类回合沉淀成知识。
"""

from __future__ import annotations

import os
import re
from typing import Any

# 可沉淀的路由：仅两类有实质业务结论的分支（clarify / fallback 无答案可言）
SEDIMENT_ROUTES = ("rag_answer", "agent_loop")

# 过短问句无沉淀价值（"确认""好的""多少钱"等）
MIN_QUESTION_CHARS = 6

# 隐私特征：问句里出现即判定为个人专属问题，不具备通用性。
# 注意不能用 \b —— 中文属于 Unicode 词字符，"手机号是13800138000"这类
# 汉字与数字相邻的写法不会产生词边界，会漏判；改用「前后非数字」断言。
_PRIVACY_PATTERNS = (
    re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"),  # 身份证号
    re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),  # 手机号
    re.compile(r"(?<!\d)\d{16,19}(?!\d)"),  # 银行卡号
)

# 纯审批回复（"确认/取消"）：本身不构成问题，需回溯上一轮诉求
_APPROVAL_REPLY_RE = re.compile(
    r"^(确认|确定|同意|不同意|取消|不要了|算了|好的|好|可以|行|是|嗯|y|yes|no)"
    r"[。！!，,.、\s]*$",
    re.IGNORECASE,
)


def faq_sediment_enabled() -> bool:
    """沉淀能力总开关（CONG_FAQ_SEDIMENT=0/false/no/off 可整体关闭）。"""
    return os.getenv("CONG_FAQ_SEDIMENT", "1").strip().lower() not in ("0", "false", "no", "off")


def evaluate_sediment_gate(state: dict[str, Any]) -> tuple[bool, str]:
    """判定本轮是否值得沉淀，返回 ``(是否通过, 未通过原因)``。

    通过则原因为空串；原因码同时写入 trace，便于评测断言与问题定位。
    """
    if not faq_sediment_enabled():
        return False, "sediment_disabled"

    # 主图状态用 intent_route，子图状态用 route：两处共用同一判定口径
    route = str(state.get("intent_route") or state.get("route") or "")
    if route not in SEDIMENT_ROUTES:
        return False, "route_not_sedimentable"

    answer = str(state.get("final_answer") or state.get("chat_response") or "").strip()
    if not answer:
        return False, "empty_answer"

    if str(state.get("fallback_reason") or "").strip():
        return False, "fallback_turn"

    metadata = state.get("metadata")
    if isinstance(metadata, dict) and metadata.get("pending_confirmation"):
        # 本轮只推送了确认卡片，业务尚未真正执行，无解决方案可沉淀
        return False, "pending_confirmation"

    if route == "rag_answer":
        if not state.get("sources"):
            return False, "no_evidence"
    elif not _has_successful_skill(state.get("tool_traces")):
        return False, "no_tool_result"

    question = resolve_sediment_question(state)
    if len(re.sub(r"\s+", "", question)) < MIN_QUESTION_CHARS:
        return False, "question_too_short"
    if _has_privacy(question):
        return False, "privacy_risk"

    return True, ""


def resolve_sediment_question(state: dict[str, Any]) -> str:
    """本轮用于沉淀的问句。

    人工确认轮（用户只回"确认/取消"）本身不构成问题，直接沉淀会产出
    "确认"这类无意义条目；此处回溯到上一轮的业务诉求
    （"帮我把套餐升级成199档"），让沉淀结果真正可复用。
    """
    question = str(state.get("rewritten_task") or state.get("task") or "").strip()
    if _is_approval_only(question):
        prior = prior_business_question(state)
        if prior:
            return prior
    return question


def prior_business_question(state: dict[str, Any]) -> str:
    """从短期会话窗口中回溯最近一条真实业务问句（跳过审批回复与本轮输入）。"""
    current = str(state.get("task") or "").strip()
    turns = state.get("recent_turns")
    if not isinstance(turns, list):
        return ""
    user_turns = [
        str(turn.get("content") or "").strip()
        for turn in turns
        if isinstance(turn, dict) and turn.get("role") == "user"
    ]
    for content in reversed(user_turns):
        if not content or content == current or _is_approval_only(content):
            continue
        return content
    return ""


def _is_approval_only(text: str) -> bool:
    return bool(_APPROVAL_REPLY_RE.match(str(text or "").strip()))


def _has_privacy(text: str) -> bool:
    return any(pattern.search(text or "") for pattern in _PRIVACY_PATTERNS)


def _has_successful_skill(traces: Any) -> bool:
    if not isinstance(traces, list):
        return False
    return any(
        isinstance(event, dict) and event.get("type") == "skill_result" and event.get("ok")
        for event in traces
    )
