"""P4-6：故障工单进度查询（只读）。"""

from __future__ import annotations

from typing import Any, ClassVar

from mokioclaw.skills.base import Skill, SkillContext, SkillParameter
from mokioclaw.skills.business_store import get_fault_ticket, list_fault_tickets


class QueryFaultStatusSkill(Skill):
    name: ClassVar[str] = "query_fault_status"
    description: ClassVar[str] = (
        "查询故障报修工单的处理进度与当前状态。"
        "用户提供工单号时按工单号精确查询；未提供工单号时查询该号码最近的工单。"
        "用户问「我的报修单怎么样了」「工单进度」「修好没有」时调用。"
    )
    access: ClassVar[str] = "read"
    parameters: ClassVar[list[SkillParameter]] = [
        SkillParameter(name="ticket_id", description="故障工单号（如 FT20261001...）", type="string", required=False),
        SkillParameter(name="phone", description="报修号码（无工单号时按号码查最近工单）", type="string", required=False),
    ]

    async def run(self, context: SkillContext, **kwargs: Any) -> dict[str, Any]:
        args = self.validate_arguments(kwargs, context)
        phone = str(args.get("phone") or context.phone)
        ticket_id = str(args.get("ticket_id") or "").strip()
        if ticket_id:
            ticket = await get_fault_ticket(ticket_id)
            return {"query": "fault_status", "lookup": "by_ticket_id", "ticket": ticket}
        tickets = await list_fault_tickets(phone)
        if not tickets:
            return {
                "query": "fault_status",
                "lookup": "by_phone",
                "phone": phone,
                "tickets": [],
                "message": f"未查询到号码 {phone} 的故障工单。",
            }
        return {"query": "fault_status", "lookup": "by_phone", "phone": phone, "tickets": tickets, "count": len(tickets)}


SKILL = QueryFaultStatusSkill()
