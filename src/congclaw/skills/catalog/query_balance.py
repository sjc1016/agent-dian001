"""P4-2：余额 / 实时话费查询（只读）。"""

from __future__ import annotations

from typing import Any, ClassVar

from congclaw.skills.base import Skill, SkillContext, SkillParameter
from congclaw.skills.business_store import get_account


class QueryBalanceSkill(Skill):
    name: ClassVar[str] = "query_balance"
    description: ClassVar[str] = (
        "查询指定电信号码的账户可用余额与本月实时话费。"
        "用户问「话费余额」「还剩多少钱」「本月话费多少」时调用。"
        "未提供号码时默认查询当前会话绑定的号码。"
    )
    access: ClassVar[str] = "read"
    parameters: ClassVar[list[SkillParameter]] = [
        SkillParameter(name="phone", description="要查询的手机号码", type="string", required=False),
    ]

    async def run(self, context: SkillContext, **kwargs: Any) -> dict[str, Any]:
        args = self.validate_arguments(kwargs, context)
        phone = str(args.get("phone") or context.phone)
        account = await get_account(phone)
        return {"query": "balance", **account}


SKILL = QueryBalanceSkill()
