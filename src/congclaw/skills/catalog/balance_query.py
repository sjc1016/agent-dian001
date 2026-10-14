"""余额查询编排：确定性单步调用 ``query_balance``。

最简单的编排形态——无分支、无重试，纯粹把「选工具」这件事从 LLM 手里收回来。
"""

from __future__ import annotations

from typing import Any, ClassVar

from congclaw.skills.base import Skill, SkillContext
from congclaw.tools.base import ToolParameter


class BalanceQuerySkill(Skill):
    name: ClassVar[str] = "balance_query"
    description: ClassVar[str] = "查询账户可用余额与本月实时话费"
    intent_hint: ClassVar[str] = (
        "用户问「话费余额」「还剩多少钱」「本月话费多少」等查询自己账户金额的诉求"
    )
    access: ClassVar[str] = "read"
    parameters: ClassVar[list[ToolParameter]] = [
        ToolParameter(name="phone", description="要查询的手机号码", type="string", required=False),
    ]

    async def run(self, context: SkillContext, **kwargs: Any) -> dict[str, Any]:
        args = self.validate_arguments(kwargs, context)
        ok, account = await self.try_call_tool(
            context, "query_balance", phone=str(args.get("phone") or "")
        )
        steps = [self._step("query_balance", ok, account)]
        if not ok:
            return self._failed(
                steps, str(account.get("error") or ""), str(account.get("error_code") or "tool_error")
            )
        return self._finished(steps, {"account": account})


SKILL = BalanceQuerySkill()
