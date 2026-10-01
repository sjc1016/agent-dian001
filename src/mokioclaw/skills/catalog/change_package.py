"""P4-7：套餐变更办理（写操作，必须人工确认后才执行，P4-16）。"""

from __future__ import annotations

from typing import Any, ClassVar

from mokioclaw.skills.base import Skill, SkillContext, SkillParameter
from mokioclaw.skills.business_store import change_package


class ChangePackageSkill(Skill):
    name: ClassVar[str] = "change_package"
    description: ClassVar[str] = (
        "为用户办理套餐变更（升级或降级套餐档位）。"
        "用户明确说「帮我改套餐」「换成 199 档」「办理这个套餐」时调用，"
        "target_package 传套餐名称或档位关键字（如「199 元档」「P199」）。"
        "本工具属于高危写操作，系统会先向用户推送确认，用户明确确认后才真正执行，"
        "因此本工具只负责执行，调用即视为已获确认。"
    )
    access: ClassVar[str] = "write"
    requires_confirmation: ClassVar[bool] = True
    parameters: ClassVar[list[SkillParameter]] = [
        SkillParameter(
            name="target_package",
            description="目标套餐名称、档位关键字或套餐编号（如 199 元档 / 5G畅享199元档 / P199）",
            type="string",
            required=True,
        ),
        SkillParameter(name="phone", description="要办理变更的手机号码", type="string", required=False),
    ]

    async def run(self, context: SkillContext, **kwargs: Any) -> dict[str, Any]:
        args = self.validate_arguments(kwargs, context)
        phone = str(args.get("phone") or context.phone)
        result = await change_package(phone, str(args["target_package"]))
        return {"action": "change_package", **result}

    def confirmation_summary(self, context: SkillContext, args: dict[str, Any]) -> str:
        """人工确认卡片文案（act 节点落 pending approval 时使用）。"""
        phone = str(args.get("phone") or context.phone)
        target = str(args.get("target_package") or "").strip()
        return (
            f"您正在为号码 {phone} 申请套餐变更，目标套餐：{target}。"
            "套餐变更将于次月 1 日生效，当月原套餐资费不变。"
            "请确认是否办理？（回复「确认」继续办理，回复「取消」放弃本次变更）"
        )


SKILL = ChangePackageSkill()
