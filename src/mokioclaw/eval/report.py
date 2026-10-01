"""P6-6：生成 Markdown 评测报告。

报告内容：
- 概览：通过率、样本数、LLM-Judge 分项平均分
- 规则校验结果表（每条样本）
- LLM-Judge 打分表
- 失败用例归因（含原始轨迹回链）
- 输出到 ``eval/reports/`` 目录
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mokioclaw.eval.llm_judge import JudgeScores
from mokioclaw.eval.normalizer import EvalSample
from mokioclaw.eval.rule_checks import RuleCheckReport


@dataclass
class SampleEvalResult:
    """单条样本的完整评测结果（规则 + LLM-Judge）。"""

    sample: EvalSample
    rule_report: RuleCheckReport
    judge_scores: JudgeScores | None = None


@dataclass
class EvalReport:
    """整体评测报告数据。"""

    report_id: str
    created_at: str
    total: int = 0
    passed: int = 0
    results: list[SampleEvalResult] = field(default_factory=list)

    @property
    def pass_rate(self) -> float:
        return round(self.passed / self.total * 100, 1) if self.total else 0.0

    @property
    def failures(self) -> list[SampleEvalResult]:
        return [r for r in self.results if not r.rule_report.passed]

    def average_scores(self) -> dict[str, float]:
        judged = [r for r in self.results if r.judge_scores is not None]
        if not judged:
            return {"answer_quality": 0.0, "reasoning": 0.0, "compliance": 0.0, "average": 0.0}
        n = len(judged)
        return {
            "answer_quality": round(sum(r.judge_scores.answer_quality for r in judged) / n, 2),
            "reasoning": round(sum(r.judge_scores.reasoning for r in judged) / n, 2),
            "compliance": round(sum(r.judge_scores.compliance for r in judged) / n, 2),
            "average": round(sum(r.judge_scores.average for r in judged) / n, 2),
        }


def render_markdown(report: EvalReport, *, reports_dir: str | Path = "eval/reports") -> str:
    """把 EvalReport 渲染为 Markdown 文本。"""
    lines: list[str] = []
    lines.append(f"# 电信客服智能体评测报告")
    lines.append("")
    lines.append(f"- 报告 ID：`{report.report_id}`")
    lines.append(f"- 生成时间：{report.created_at}")
    lines.append(f"- 样本总数：{report.total}")
    lines.append(f"- 通过数：{report.passed}")
    lines.append(f"- **规则校验通过率：{report.pass_rate}%**")
    lines.append("")

    # LLM-Judge 分项得分
    avg = report.average_scores()
    lines.append("## LLM-Judge 分项平均分")
    lines.append("")
    lines.append("| 维度 | 平均分 |")
    lines.append("| --- | --- |")
    lines.append(f"| 答案质量 | {avg['answer_quality']} |")
    lines.append(f"| 推理逻辑 | {avg['reasoning']} |")
    lines.append(f"| 话术合规 | {avg['compliance']} |")
    lines.append(f"| **综合** | **{avg['average']}** |")
    lines.append("")

    # 规则校验结果表
    lines.append("## 规则校验结果")
    lines.append("")
    lines.append("| 样本 | 输入 | 期望类别 | 实际类别 | 分流 | 工具参数 | 兜底 | 追问 | 通过 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for r in report.results:
        s = r.sample
        rule_map = {res.name: res for res in r.rule_report.results}
        mark = lambda b: "✅" if b else "❌"
        lines.append(
            f"| {s.sample_id} | {_truncate(s.input, 30)} | {s.expected_category} | "
            f"{s.actual_category} | {mark(rule_map['routing'].passed)} | "
            f"{mark(rule_map['tool_params'].passed)} | {mark(rule_map['fallback'].passed)} | "
            f"{mark(rule_map['clarify_rounds'].passed)} | {mark(r.rule_report.passed)} |"
        )
    lines.append("")

    # 失败用例归因
    if report.failures:
        lines.append("## 失败用例归因")
        lines.append("")
        for r in report.failures:
            s = r.sample
            lines.append(f"### {s.sample_id}：{_truncate(s.input, 60)}")
            lines.append("")
            lines.append(f"- 期望：`{s.expected_category}` → `{s.expected_route}`，"
                         f"工具 `{s.expected_tools or '无'}`，兜底 `{s.expected_fallback}`")
            lines.append(f"- 实际：`{s.actual_category}` → `{s.actual_route}`，"
                         f"工具 `{s.actual_tools or '无'}`，兜底 `{s.actual_fallback}`")
            lines.append(f"- 失败规则：")
            for fail in r.rule_report.failures:
                lines.append(f"  - **{fail.name}**：{fail.detail}")
            trace_link = s.events_file.replace("\\", "/")
            lines.append(f"- 轨迹回链：`{trace_link}`")
            lines.append("")
    else:
        lines.append("## 失败用例归因")
        lines.append("")
        lines.append("全部样本规则校验通过 🎉")
        lines.append("")

    # LLM-Judge 详表
    judged = [r for r in report.results if r.judge_scores is not None]
    if judged:
        lines.append("## LLM-Judge 详表")
        lines.append("")
        lines.append("| 样本 | 答案质量 | 推理 | 合规 | 综合 | 评语 |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for r in judged:
            j = r.judge_scores
            comment = _truncate(j.answer_quality_comment, 40)
            lines.append(
                f"| {r.sample.sample_id} | {j.answer_quality} | {j.reasoning} | "
                f"{j.compliance} | {j.average} | {comment} |"
            )
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def write_report(report: EvalReport, reports_dir: str | Path = "eval/reports") -> Path:
    """把报告写入 eval/reports/{report_id}.md 与同名 .json。"""
    out_dir = Path(reports_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / f"{report.report_id}.md"
    json_path = out_dir / f"{report.report_id}.json"

    md_path.write_text(render_markdown(report, reports_dir=reports_dir), encoding="utf-8")

    json_data = {
        "report_id": report.report_id,
        "created_at": report.created_at,
        "total": report.total,
        "passed": report.passed,
        "pass_rate": report.pass_rate,
        "average_scores": report.average_scores(),
        "samples": [
            {
                "sample_id": r.sample.sample_id,
                "input": r.sample.input,
                "expected_category": r.sample.expected_category,
                "actual_category": r.sample.actual_category,
                "rule_passed": r.rule_report.passed,
                "rule_failures": [
                    {"name": f.name, "detail": f.detail} for f in r.rule_report.failures
                ],
                "judge": (
                    {
                        "answer_quality": r.judge_scores.answer_quality,
                        "reasoning": r.judge_scores.reasoning,
                        "compliance": r.judge_scores.compliance,
                        "average": r.judge_scores.average,
                        "fallback": r.judge_scores.fallback,
                    }
                    if r.judge_scores
                    else None
                ),
                "trace_id": r.sample.trace_id,
                "events_file": r.sample.events_file,
            }
            for r in report.results
        ],
    }
    json_path.write_text(json.dumps(json_data, ensure_ascii=False, indent=2), encoding="utf-8")
    return md_path


def _truncate(text: str, limit: int) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def new_report_id() -> str:
    """生成报告 ID。"""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"eval-{stamp}"
