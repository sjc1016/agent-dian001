"""阶段 6 评测流水线运行器。

串联 collector → normalizer → rule_checks → trace_checks → llm_judge → report。

- :func:`run_evaluation`：一键跑完整评测集，生成报告
- :func:`run_single_sample`：跑单条样本（供 API 与调试用）
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from congclaw.core.agent import stream_session_events_async
from congclaw.eval.collector import collect_from_events, find_latest_trace_dir
from congclaw.eval.dataset import LABELED_DATASET, get_dataset
from congclaw.eval.llm_judge import judge_answer
from congclaw.eval.normalizer import EvalSample, normalize
from congclaw.eval.report import (
    EvalReport,
    SampleEvalResult,
    new_report_id,
    write_report,
)
from congclaw.eval.rule_checks import run_rule_checks
from congclaw.eval.trace_checks import run_trace_checks
from congclaw.eval.trace_metrics import compute_metrics

WORKSPACE_KEEP = 3  # 评测临时工作区保留最近 N 次，更早的自动清理


def prune_eval_workspaces(
    keep: int = WORKSPACE_KEEP,
    temp_root: str | Path | None = None,
) -> list[Path]:
    """清理历史评测临时工作区，只保留最近 keep 个（含本次）。

    报告的「轨迹回链」指向这些目录里的 events.jsonl，因此保留最近若干次而非跑完即删；
    更早的目录连同临时空间一并回收，避免每次评测都留下一批无人清理的目录。
    """
    root = Path(temp_root) if temp_root is not None else Path(tempfile.gettempdir())
    if not root.is_dir():
        return []

    candidates: list[tuple[float, Path]] = []
    for path in root.glob("cong-eval-*"):
        if not path.is_dir():
            continue
        try:
            candidates.append((path.stat().st_mtime, path))
        except OSError:
            continue
    if len(candidates) <= keep:
        return []

    candidates.sort(reverse=True)
    removed: list[Path] = []
    for _, path in candidates[keep:]:
        try:
            shutil.rmtree(path)
        except OSError:  # 目录被占用或权限不足时跳过，不影响评测结果
            continue
        removed.append(path)
    return removed


async def run_evaluation(
    *,
    use_llm_judge: bool = True,
    reports_dir: str | Path = "eval/reports",
    dataset: list[dict[str, Any]] | None = None,
    base_workspace: str | Path | None = None,
) -> EvalReport:
    """一键跑完整评测集，生成 Markdown + JSON 报告。

    参数：
    - use_llm_judge: 是否启用 LLM-Judge（模型不可用时自动退化）
    - reports_dir: 报告输出目录
    - dataset: 自定义评测集，默认使用内置 30 条
    - base_workspace: 评测用工作区根目录，默认系统临时目录
      （临时目录场景下只保留最近 :data:`WORKSPACE_KEEP` 次，更早的自动清理）
    """
    samples = dataset if dataset is not None else get_dataset()
    base = Path(base_workspace) if base_workspace else Path(tempfile.mkdtemp(prefix="cong-eval-"))
    base.mkdir(parents=True, exist_ok=True)

    report = EvalReport(
        report_id=new_report_id(),
        created_at=datetime.now(timezone.utc).isoformat(),
        total=len(samples),
    )

    for idx, labeled in enumerate(samples, start=1):
        sample_id = str(labeled.get("id") or f"S{idx}")
        workspace = base / sample_id
        try:
            result = await run_single_sample(
                labeled,
                workspace=workspace,
                use_llm_judge=use_llm_judge,
            )
            report.results.append(result)
            if result.rule_report.passed:
                report.passed += 1
        except Exception as exc:  # noqa: BLE001 —— 单条失败不中断整体
            report.results.append(
                SampleEvalResult(
                    sample=EvalSample(
                        sample_id=sample_id,
                        input=str(labeled.get("input") or ""),
                        expected_category=str(labeled.get("expected_category") or ""),
                        expected_route=str(labeled.get("expected_route") or ""),
                        expected_tools=list(labeled.get("expected_tools") or []),
                        expected_fallback=str(labeled.get("expected_fallback") or "none"),
                        actual_category="error",
                        actual_route="error",
                        actual_tools=[],
                        actual_answer="",
                        actual_fallback="error",
                        clarify_rounds=0,
                        notes=f"evaluation error: {type(exc).__name__}: {exc}",
                    ),
                    rule_report=_error_rule_report(sample_id, str(exc)),
                )
            )

    write_report(report, reports_dir=reports_dir)
    prune_eval_workspaces()
    return report


async def run_single_sample(
    labeled: dict[str, Any],
    *,
    workspace: str | Path,
    use_llm_judge: bool = True,
) -> SampleEvalResult:
    """跑单条样本：调用对话 → 采集轨迹 → 归一化 → 规则校验 → 链路断言 → LLM-Judge。"""
    ws = Path(workspace)
    ws.mkdir(parents=True, exist_ok=True)

    events: list[dict[str, Any]] = []
    async for event in stream_session_events_async(
        str(labeled.get("input") or ""),
        session_workspace=ws,
        max_attempts=3,
        approval_mode="auto",  # 评测时自动批准写操作，避免人工确认阻塞
        phone=os.getenv("EVAL_PHONE", "13800138000"),
    ):
        events.append(event)

    # 从流事件构建轨迹；同时定位 trace 文件用于回链
    trace_dir = find_latest_trace_dir(ws)
    trace = collect_from_events(events, trace_id=trace_dir.name if trace_dir else "in-memory")
    if trace_dir:
        trace.events_file = str(trace_dir / "events.jsonl")

    sample = normalize(trace, labeled)
    rule_report = run_rule_checks(sample)
    trace_report = run_trace_checks(trace)
    metrics = compute_metrics(trace)

    judge_scores = None
    if use_llm_judge:
        judge_scores = await judge_answer(sample)

    return SampleEvalResult(
        sample=sample,
        rule_report=rule_report,
        judge_scores=judge_scores,
        trace_report=trace_report,
        metrics=metrics,
    )


def _error_rule_report(sample_id: str, error: str) -> Any:
    """样本执行异常时的占位规则报告。"""
    from congclaw.eval.rule_checks import RuleCheckReport, RuleResult

    return RuleCheckReport(
        sample_id=sample_id,
        passed=False,
        results=[
            RuleResult(
                name="execution",
                passed=False,
                detail=f"sample execution failed: {error}",
            )
        ],
    )


def run_evaluation_sync(**kwargs) -> EvalReport:
    """同步入口（供非 async 上下文调用，如 CLI）。"""
    return asyncio.run(run_evaluation(**kwargs))
