"""P4-3：当前套餐与余量查询（只读）。"""

from __future__ import annotations

from typing import Any, ClassVar

from congclaw.skills.base import Skill, SkillContext, SkillParameter
from congclaw.skills.business_store import get_user_package_detail


class QueryPackageSkill(Skill):
    name: ClassVar[str] = "query_package"
    description: ClassVar[str] = (
        "查询指定电信号码当前在用的套餐名称、月费，以及流量/语音余量。"
        "用户问「我现在用的什么套餐」「流量还剩多少」「套餐余量」时调用。"
        "未提供号码时默认查询当前会话绑定的号码。"
    )
    access: ClassVar[str] = "read"
    parameters: ClassVar[list[SkillParameter]] = [
        SkillParameter(name="phone", description="要查询的手机号码", type="string", required=False),
    ]

    async def run(self, context: SkillContext, **kwargs: Any) -> dict[str, Any]:
        args = self.validate_arguments(kwargs, context)
        phone = str(args.get("phone") or context.phone)
        detail = await get_user_package_detail(phone)
        return {"query": "user_package", **detail}


SKILL = QueryPackageSkill()
