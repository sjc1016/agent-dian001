"""故障报修编排：建单 → 按工单号复核落库（确定性顺序，无 LLM 参与控制流）。

故障类型不强制模型输出枚举值——由本编排按关键词确定性归一，避免模型用词
与枚举不一致时整轮失败。
"""

from __future__ import annotations

from typing import Any, ClassVar

from congclaw.domain.business_store import FAULT_TYPES
from congclaw.skills.base import Skill, SkillContext
from congclaw.tools.base import ToolParameter

# 关键词 → 故障类型（按序匹配，命中即止）
_FAULT_KEYWORDS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("iptv", "机顶盒", "电视"), "IPTV故障"),
    (("宽带", "光猫", "断网", "wifi", "wi-fi", "无线", "网线", "上网", "网络"), "宽带故障"),
    (("通话", "打电话", "无法拨", "拨不出", "语音"), "通话故障"),
    (("手机", "信号", "移动数据", "流量", "5g", "4g"), "手机网络故障"),
)


def normalize_fault_type(raw: str, description: str) -> str:
    """把模型给的故障类型归一到 ``FAULT_TYPES`` 枚举：先精确匹配，再按描述推断。"""
    candidate = str(raw or "").strip()
    if candidate in FAULT_TYPES:
        return candidate
    haystack = f"{candidate} {description}".lower()
    for keywords, fault_type in _FAULT_KEYWORDS:
        if any(keyword in haystack for keyword in keywords):
            return fault_type
    return "其他"


class FaultRepairSkill(Skill):
    name: ClassVar[str] = "fault_repair"
    description: ClassVar[str] = "登记故障报修工单（宽带断网/手机无信号/通话异常/IPTV 无法观看）"
    intent_hint: ClassVar[str] = (
        "用户描述自己正在遭遇的故障（断网、光猫亮红灯、没信号、IPTV 看不了）并要求报修或派人维修"
    )
    access: ClassVar[str] = "write"
    parameters: ClassVar[list[ToolParameter]] = [
        ToolParameter(
            name="fault_type",
            description="故障类型（如「宽带故障」「手机网络故障」），不确定时留空由系统按故障描述判定",
            type="string",
            required=False,
        ),
        ToolParameter(
            name="description",
            description="故障现象描述（如：从昨晚开始光猫亮红灯，全屋无法上网）",
            type="string",
            required=True,
        ),
        ToolParameter(name="phone", description="故障关联号码/报修号码", type="string", required=False),
        ToolParameter(
            name="address",
            description="上门维修地址（宽带/IPTV 故障需要）",
            type="string",
            required=False,
        ),
        ToolParameter(name="contact", description="联系电话（默认同报修号码）", type="string", required=False),
    ]

    async def run(self, context: SkillContext, **kwargs: Any) -> dict[str, Any]:
        args = self.validate_arguments(kwargs, context)
        description = str(args["description"])
        phone = str(args.get("phone") or "")
        fault_type = normalize_fault_type(str(args.get("fault_type") or ""), description)

        # 步骤 1：建单
        ok, ticket = await self.try_call_tool(
            context,
            "report_fault",
            fault_type=fault_type,
            description=description,
            phone=phone,
            address=str(args.get("address") or ""),
            contact=str(args.get("contact") or ""),
        )
        steps = [self._step("report_fault", ok, ticket)]
        if not ok:
            return self._failed(
                steps,
                str(ticket.get("error") or ""),
                str(ticket.get("error_code") or "tool_error"),
            )

        # 步骤 2：按工单号复核已落库（尽力而为：复核失败不改判建单结果）
        ticket_id = str(ticket.get("ticket_id") or "")
        verified, verify = await self.try_call_tool(context, "query_fault_status", ticket_id=ticket_id)
        steps.append(self._step("query_fault_status", verified, verify))
        return self._finished(
            steps,
            {
                "ticket": ticket,
                "verified": verified,
                "fault_type": fault_type,
                "phone": phone or ticket.get("phone", ""),
            },
        )


SKILL = FaultRepairSkill()
