"""P6-8：评测流水线 API。

- ``POST /api/v1/eval/run``：触发评测流水线，返回报告 ID 与摘要
- ``GET /api/v1/eval/reports/{id}``：获取评测报告（Markdown / JSON）
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from congclaw.eval.runner import run_evaluation

router = APIRouter(prefix="/api/v1/eval", tags=["eval"])

REPORTS_DIR = Path("eval/reports")


class EvalRunRequest(BaseModel):
    """评测触发请求。"""

    use_llm_judge: bool = Field(True, description="是否启用 LLM-Judge 打分")
    sample_ids: list[str] | None = Field(
        None, description="指定评测样本 ID 列表，不填则跑全部 30 条"
    )


@router.post("/run")
async def run_eval(request: EvalRunRequest) -> dict[str, Any]:
    """触发评测流水线，生成报告并返回摘要。"""
    from congclaw.eval.dataset import LABELED_DATASET

    dataset = LABELED_DATASET
    if request.sample_ids:
        wanted = set(request.sample_ids)
        dataset = [s for s in LABELED_DATASET if s.get("id") in wanted]
        if not dataset:
            raise HTTPException(status_code=400, detail="no matching sample ids")

    report = await run_evaluation(
        use_llm_judge=request.use_llm_judge,
        reports_dir=REPORTS_DIR,
        dataset=dataset,
    )
    return {
        "report_id": report.report_id,
        "created_at": report.created_at,
        "total": report.total,
        "passed": report.passed,
        "pass_rate": report.pass_rate,
        "average_scores": report.average_scores(),
        "trace_summary": report.trace_summary(),
        "report_path": str(REPORTS_DIR / f"{report.report_id}.md"),
    }


@router.get("/reports/{report_id}")
async def get_report(
    report_id: str,
    format: str = Query("markdown", description="markdown 或 json"),
) -> dict[str, Any]:
    """获取评测报告内容。"""
    if format == "json":
        path = REPORTS_DIR / f"{report_id}.json"
    else:
        path = REPORTS_DIR / f"{report_id}.md"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"report {report_id} not found")
    content = path.read_text(encoding="utf-8")
    return {"report_id": report_id, "format": format, "content": content}


@router.get("/reports")
async def list_reports() -> dict[str, Any]:
    """列出所有评测报告。"""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    reports = []
    for path in sorted(REPORTS_DIR.glob("*.json"), reverse=True):
        try:
            import json

            data = json.loads(path.read_text(encoding="utf-8"))
            reports.append(
                {
                    "report_id": data.get("report_id"),
                    "created_at": data.get("created_at"),
                    "total": data.get("total"),
                    "passed": data.get("passed"),
                    "pass_rate": data.get("pass_rate"),
                }
            )
        except Exception:
            continue
    return {"reports": reports, "count": len(reports)}
