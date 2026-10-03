"""P6-6：生成 Markdown 评测报告。

报告内容：
- 概览：通过率、样本数、LLM-Judge 分项平均分
- 规则校验结果表（每条样本）
- 链路校验（trace 结构断言，独立于通过率）
- 链路指标（数值观测值，独立于通过率）
- 失败用例归因（含原始轨迹回链）
- LLM-Judge 打分表
- 输出到 ``eval/reports/`` 目录
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from congclaw.eval.llm_judge import JudgeScores
from congclaw.eval.normalizer import EvalSample
from congclaw.eval.rule_checks import RuleCheckReport
from congclaw.eval.trace_checks import TraceCheckReport
from congclaw.eval.trace_metrics import METRIC_LABELS, TraceMetrics, aggregate_metrics


@dataclass
class SampleEvalResult:
    """单条样本的完整评测结果（规则 + 链路断言 + 链路指标 + LLM-Judge）。"""

    sample: EvalSample
    rule_report: RuleCheckReport
    judge_scores: JudgeScores | None = None
    trace_report: TraceCheckReport | None = None
    metrics: TraceMetrics | None = None


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

    @property
    def trace_checked(self) -> list[SampleEvalResult]:
        """链路断言适用的样本。"""
        return [
            r for r in self.results if r.trace_report is not None and r.trace_report.applicable
        ]

    @property
    def trace_failures(self) -> list[SampleEvalResult]:
        return [r for r in self.trace_checked if not r.trace_report.passed]

    def trace_summary(self) -> dict[str, int]:
        """链路校验汇总（独立口径，不影响 pass_rate）。"""
        checked = self.trace_checked
        return {
            "total": self.total,
            "applicable": len(checked),
            "passed": len(checked) - len(self.trace_failures),
            "failed": len(self.trace_failures),
        }

    def trace_assertion_failures(self) -> dict[str, int]:
        """按断言名统计失败样本数，定位链路上最脆弱的一环。"""
        counter: Counter[str] = Counter()
        for result in self.trace_failures:
            for failure in result.trace_report.failures:
                counter[failure.name] += 1
        return dict(counter.most_common())

    def trace_metrics_summary(self) -> dict[str, dict[str, float]]:
        """链路指标聚合（观测值口径，不做阈值判定、不影响 pass_rate）。

        每项含 mean / min / max / n，其中 n 是该指标真正适用的样本数
        （链路里没走到这一步的样本不计入），故不同指标的 n 可以不同。
        """
        return aggregate_metrics(
            result.metrics for result in self.results if result.metrics is not None
        )


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

    # 链路校验（独立口径，不并入通过率）
    lines.extend(_render_trace_section(report))

    # 链路指标（观测值，同样不并入通过率）
    lines.extend(_render_metrics_section(report))

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


def _render_trace_section(report: EvalReport) -> list[str]:
    """渲染「链路校验」小节：结构断言与规则通过率各论各的。"""
    summary = report.trace_summary()
    lines = ["## 链路校验（trace 结构断言，不计入通过率）", ""]
    lines.append(f"- 适用样本：{summary['applicable']} / {summary['total']}")
    if not summary["applicable"]:
        lines.append("- 本次评测没有可校验的 RAG / Agent 链路轨迹")
        lines.append("")
        return lines

    lines.append(f"- 链路全部通过：{summary['passed']}")
    lines.append(f"- 链路失败：{summary['failed']}")
    lines.append("")
    lines.append("| 样本 | 链路 | 失败断言 |")
    lines.append("| --- | --- | --- |")
    for result in report.trace_checked:
        failed = result.trace_report.failures
        detail = "；".join(f"`{item.name}`：{item.detail}" for item in failed) if failed else "-"
        lines.append(f"| {result.sample.sample_id} | {'❌' if failed else '✅'} | {detail} |")
    lines.append("")

    assertion_failures = report.trace_assertion_failures()
    if assertion_failures:
        lines.append("### 断言失败分布")
        lines.append("")
        lines.append("| 断言 | 失败样本数 |")
        lines.append("| --- | --- |")
        for name, count in assertion_failures.items():
            lines.append(f"| `{name}` | {count} |")
        lines.append("")
    return lines


def _render_metrics_section(report: EvalReport) -> list[str]:
    """渲染「链路指标」小节：只给观测值（均值/极值/样本数），不做阈值判定。"""
    summary = report.trace_metrics_summary()
    lines = ["## 链路指标（观测值，不做阈值判定、不计入通过率）", ""]
    if not summary:
        lines.append("- 本次评测没有可计算的链路指标（无 RAG / Agent 链路轨迹或缺少落盘 trace）")
        lines.append("")
        return lines

    lines.append("| 指标 | 均值 | 最小 | 最大 | 样本数 |")
    lines.append("| --- | --- | --- | --- | --- |")
    for name, stat in summary.items():
        lines.append(
            f"| {METRIC_LABELS.get(name, name)} | {stat['mean']} | {stat['min']} | "
            f"{stat['max']} | {stat['n']} |"
        )
    lines.append("")
    lines.append("> 「样本数」是该指标真正适用的样本数（链路里没走到这一步的样本不计入），"
                 "因此不同指标的样本数可以不同；耗时类指标依赖落盘 trace，缺失即不统计。")
    lines.append("")
    return lines


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
        "trace_summary": report.trace_summary(),
        "trace_assertion_failures": report.trace_assertion_failures(),
        "trace_metrics": report.trace_metrics_summary(),
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
                "trace": (
                    {
                        "applicable": r.trace_report.applicable,
                        "passed": r.trace_report.passed,
                        "failures": [
                            {"name": f.name, "title": f.title, "detail": f.detail}
                            for f in r.trace_report.failures
                        ],
                    }
                    if r.trace_report
                    else None
                ),
                "metrics": r.metrics.to_dict() if r.metrics else None,
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
