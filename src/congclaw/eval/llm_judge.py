"""P6-5：LLM-Judge——对答案质量、推理逻辑、话术合规性打分。

三个维度各 1~5 分 + 评语：
- answer_quality：答案是否准确、完整、有帮助
- reasoning：推理/工具调用是否合理、链路是否自洽
- compliance：话术是否符合电信客服规范（礼貌、无越权承诺、引导合法）

复用 :func:`congclaw.providers.openai_provider.create_model`。
模型不可用时退化为规则打分（兜底/追问场景给默认分），保证评测流水线不中断。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from congclaw.eval.normalizer import EvalSample

JUDGE_PROMPT = """你是电信客服智能体的评测裁判。请根据以下信息对本轮回答打分。

【用户输入】
{input}

【期望意图】
{expected_category}（路由：{expected_route}）

【实际轨迹摘要】
- 实际意图：{actual_category}（路由：{actual_route}）
- 调用工具：{actual_tools}
- 反思决策：{reflect_decision}
- RAG 命中数：{rag_hit_count}

【实际回答】
{actual_answer}

请从三个维度各打 1~5 分（1=很差，5=优秀），并给出简短评语。
只输出 JSON，不要输出其他内容：
{{
  "answer_quality": 1-5,
  "reasoning": 1-5,
  "compliance": 1-5,
  "answer_quality_comment": "...",
  "reasoning_comment": "...",
  "compliance_comment": "..."
}}
"""


@dataclass
class JudgeScores:
    """LLM-Judge 打分结果。"""

    answer_quality: int = 0
    reasoning: int = 0
    compliance: int = 0
    answer_quality_comment: str = ""
    reasoning_comment: str = ""
    compliance_comment: str = ""
    fallback: bool = False  # 是否因模型不可用而退化打分

    @property
    def total(self) -> int:
        return self.answer_quality + self.reasoning + self.compliance

    @property
    def average(self) -> float:
        return round(self.total / 3, 2)


async def judge_answer(sample: EvalSample) -> JudgeScores:
    """对单条样本执行 LLM-Judge 打分。

    模型不可用时退化为规则打分，保证评测流水线不中断。
    """
    prompt = JUDGE_PROMPT.format(
        input=sample.input,
        expected_category=sample.expected_category,
        expected_route=sample.expected_route,
        actual_category=sample.actual_category,
        actual_route=sample.actual_route,
        actual_tools=sample.actual_tools or ["(无)"],
        reflect_decision=sample.reflect_decision or "(无)",
        rag_hit_count=sample.rag_hit_count,
        actual_answer=sample.actual_answer or "(空)",
    )
    try:
        from congclaw.providers.openai_provider import create_model
        from langchain_core.messages import HumanMessage, SystemMessage

        model = create_model()
        response = await model.ainvoke(
            [SystemMessage(content="你是严格的客服对话评测裁判。"), HumanMessage(content=prompt)]
        )
        content = str(getattr(response, "content", "") or "").strip()
        parsed = _extract_json(content)
        if parsed:
            return _scores_from_dict(parsed)
    except Exception:
        pass

    # 退化：规则打分
    return _fallback_scores(sample)


def _fallback_scores(sample: EvalSample) -> JudgeScores:
    """模型不可用时的规则退化打分。"""
    scores = JudgeScores(fallback=True)
    # 路由正确给基础分
    routing_ok = sample.actual_category == sample.expected_category
    base = 3 if routing_ok else 1

    scores.answer_quality = base if sample.actual_answer else 1
    scores.answer_quality_comment = "模型不可用，规则退化打分"

    # 推理：工具调用与期望匹配则给分
    if sample.expected_tools:
        tools_match = set(sample.expected_tools) <= set(sample.actual_tools)
        scores.reasoning = base if tools_match else 1
    else:
        scores.reasoning = base
    scores.reasoning_comment = "模型不可用，规则退化打分"

    # 合规：有兜底且非业务类给分；业务类无兜底给分
    if sample.expected_fallback != "none":
        scores.compliance = 4 if sample.actual_fallback != "none" else 1
    else:
        scores.compliance = 3 if sample.actual_fallback == "none" else 1
    scores.compliance_comment = "模型不可用，规则退化打分"
    return scores


def _extract_json(text: str) -> dict[str, Any] | None:
    """从模型输出中提取 JSON 对象。"""
    text = text.strip()
    # 直接尝试
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # 提取第一个 {...}
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    return None


def _scores_from_dict(data: dict[str, Any]) -> JudgeScores:
    def _score(key: str) -> int:
        value = data.get(key, 0)
        try:
            value = int(value)
        except (TypeError, ValueError):
            value = 0
        return max(1, min(5, value))

    return JudgeScores(
        answer_quality=_score("answer_quality"),
        reasoning=_score("reasoning"),
        compliance=_score("compliance"),
        answer_quality_comment=str(data.get("answer_quality_comment") or ""),
        reasoning_comment=str(data.get("reasoning_comment") or ""),
        compliance_comment=str(data.get("compliance_comment") or ""),
        fallback=False,
    )
