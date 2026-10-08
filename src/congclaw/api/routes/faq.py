"""阶段 7：常见问答沉淀库管理路由。

沉淀条目由主图 ``faq_sediment`` 节点自动产出（默认 draft 待审），本路由提供
按状态/分类/关键词检索、规模统计与人工审核流转：

- ``GET  /api/v1/faq``                 分页查询沉淀条目
- ``GET  /api/v1/faq/stats``           规模统计与高频条目
- ``GET  /api/v1/faq/{faq_id}``        单条详情
- ``PUT  /api/v1/faq/{faq_id}/status`` 审核流转（draft → approved / published / archived）
- ``POST /api/v1/faq/reflow``         手动重跑回流（状态流转之外的兜底）

状态流转触及 published 集合时（发布，或把已发布条目归档下架），额外把全部已发布
条目回流到 RAG 知识库，结果挂在响应的 ``reflow`` 字段上；回流失败不影响审核结果。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from congclaw.faq.reflow import areflow_published_faq
from congclaw.faq.store import (
    FAQ_STATUSES,
    afaq_stats,
    aget_faq,
    alist_faq,
    aupdate_faq_status,
)

router = APIRouter(prefix="/api/v1/faq", tags=["faq"])

# 触及该状态即需要重跑回流：新发布，或把已发布条目下架
REFLOW_TRIGGER_STATUS = "published"


class FaqStatusRequest(BaseModel):
    """人工审核状态流转请求体。"""

    status: str = Field(..., description=f"目标状态，可选 {list(FAQ_STATUSES)}")


@router.get("/stats")
async def stats() -> dict:
    """沉淀库规模：状态分布、分类分布与高频条目 TOP 5。"""
    return await afaq_stats()


@router.get("")
async def list_entries(
    status: str | None = Query(None, description=f"按状态筛选，可选 {list(FAQ_STATUSES)}"),
    category: str | None = Query(None, description="按分类筛选（咨询/办理/故障/规则）"),
    q: str = Query("", description="关键词，匹配标准问句/解决方案/关键词/同义问法"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict:
    """分页查询沉淀条目（默认按常见度 merge_count 降序）。"""
    if status and status not in FAQ_STATUSES:
        raise HTTPException(status_code=400, detail=f"非法状态：{status}")
    return await alist_faq(
        status=status, category=category, keyword=q, limit=limit, offset=offset
    )


@router.get("/{faq_id}")
async def get_entry(faq_id: str) -> dict:
    """按编号查询单条沉淀条目。"""
    entry = await aget_faq(faq_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"FAQ 条目不存在：{faq_id}")
    return entry


@router.put("/{faq_id}/status")
async def update_status(faq_id: str, request: FaqStatusRequest) -> dict:
    """人工审核：把条目流转到 approved / published（或 archived 下架）。

    流转涉及 published 集合时（发布、或把已发布条目下架）触发一次知识库回流，
    结果放在响应的 ``reflow`` 字段；回流失败只记录错误，不回滚审核结果。
    """
    previous = await aget_faq(faq_id)
    if previous is None:
        raise HTTPException(status_code=404, detail=f"FAQ 条目不存在：{faq_id}")
    try:
        entry = await aupdate_faq_status(faq_id, request.status)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if entry is None:
        raise HTTPException(status_code=404, detail=f"FAQ 条目不存在：{faq_id}")

    result: dict[str, Any] = dict(entry)
    # 状态未变化（重复提交同状态）不必重跑回流
    touched_published = previous.get("status") != entry.get("status") and REFLOW_TRIGGER_STATUS in (
        previous.get("status"),
        entry.get("status"),
    )
    if touched_published:
        result["reflow"] = await _safe_reflow()
    return result


@router.post("/reflow")
async def reflow() -> dict:
    """手动重跑回流：把全部已发布条目重新渲染并入库。

    状态流转之外的兜底入口——回流失败后要重试，或已发布条目被后续回合合并
    （merge）更新了内容而状态未变、因而不会自动触发回流时，用这里强制同步。
    """
    result = await _safe_reflow()
    if result.get("status") == "error":
        # 用户显式要求回流，失败必须暴露，不能像状态流转那样静默降级
        raise HTTPException(status_code=500, detail=f"回流失败：{result['error']}")
    return result


async def _safe_reflow() -> dict[str, Any]:
    """回流失败降级为错误摘要——审核结果已提交，不能因此回滚或报 5xx。"""
    try:
        return await areflow_published_faq()
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
