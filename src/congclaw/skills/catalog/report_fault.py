"""P4-5：故障报修建单（写操作，不需要人工确认——用户已明确诉求即建单）。"""

from __future__ import annotations

from typing import Any, ClassVar

from congclaw.skills.base import Skill, SkillContext, SkillParameter
from congclaw.skills.business_store import FAULT_TYPES, create_fault_ticket


class ReportFaultSkill(Skill):
    name: ClassVar[str] = "report_fault"
    description: ClassVar[str] = (
        "为用户创建电信故障报修工单（宽带断网、手机无信号、通话异常、IPTV 无法观看等）。"
        "用户明确描述故障并要求报修/派人维修时调用，返回工单号与受理状态。"
        "fault_type 必须从枚举中选择；description 需包含故障现象与持续时间。"
    )
    access: ClassVar[str] = "write"
    requires_confirmation: ClassVar[bool] = False
    parameters: ClassVar[list[SkillParameter]] = [
        SkillParameter(
            name="fault_type",
            description="故障类型",
            type="string",
            required=True,
            enum=list(FAULT_TYPES),
        ),
        SkillParameter(
            name="description",
            description="故障现象描述（如：从昨晚开始光猫亮红灯，全屋无法上网）",
            type="string",
            required=True,
        ),
        SkillParameter(name="phone", description="故障关联号码/报修号码", type="string", required=False),
        SkillParameter(name="address", description="上门维修地址（宽带/IPTV 故障需要）", type="string", required=False),
        SkillParameter(name="contact", description="联系电话（默认同报修号码）", type="string", required=False),
    ]

    async def run(self, context: SkillContext, **kwargs: Any) -> dict[str, Any]:
        args = self.validate_arguments(kwargs, context)
        phone = str(args.get("phone") or context.phone)
        ticket = await create_fault_ticket(
            phone=phone,
            fault_type=str(args["fault_type"]),
            description=str(args["description"]),
            address=str(args.get("address") or ""),
            contact=str(args.get("contact") or ""),
        )
        return {"action": "report_fault", **ticket}


SKILL = ReportFaultSkill()
