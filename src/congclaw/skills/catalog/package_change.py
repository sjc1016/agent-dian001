"""套餐变更编排：查当前套餐 → 提交变更（命中高危写操作则中止并请求人工确认）。"""

from __future__ import annotations

from typing import Any, ClassVar

from congclaw.skills.base import Skill, SkillContext
from congclaw.tools.base import ToolParameter


class PackageChangeSkill(Skill):
    name: ClassVar[str] = "package_change"
    description: ClassVar[str] = "办理套餐变更（升级/降级档位），属高危写操作，需人工确认"
    intent_hint: ClassVar[str] = (
        "用户明确要求办理、更换或升级/降级套餐档位（如「帮我把套餐改成 199 档」）"
    )
    access: ClassVar[str] = "write"
    parameters: ClassVar[list[ToolParameter]] = [
        ToolParameter(
            name="target_package",
            description="目标套餐名称、档位关键字或套餐编号（如 199 元档 / 5G畅享199元档 / P199）",
            type="string",
            required=True,
        ),
        ToolParameter(name="phone", description="要办理变更的手机号码", type="string", required=False),
    ]

    async def run(self, context: SkillContext, **kwargs: Any) -> dict[str, Any]:
        args = self.validate_arguments(kwargs, context)
        target = str(args["target_package"])
        phone = str(args.get("phone") or "")

        # 步骤 1：先查当前套餐（供确认话术与答复说明「原套餐 → 新套餐」）
        current_ok, current = await self.try_call_tool(context, "query_package", phone=phone)
        steps = [self._step("query_package", current_ok, current)]

        # 步骤 2：提交变更。需人工确认时 change_package 不执行，
        # 由 call_tool() 抛 ConfirmationRequired 交给执行节点落确认单（下一轮恢复执行）。
        # 空号码不下传，避免确认卡片出现无意义的空参数
        change_args: dict[str, Any] = {"target_package": target}
        if phone:
            change_args["phone"] = phone
        change = await self.call_tool(context, "change_package", **change_args)
        steps.append(self._step("change_package", True, change))
        return self._finished(
            steps,
            {
                "current": current if current_ok else {},
                "change": change,
                "phone": phone or str(current.get("phone") or ""),
            },
        )


SKILL = PackageChangeSkill()
