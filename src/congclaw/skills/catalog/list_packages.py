"""P4-4：可办理套餐列表查询（只读）。"""

from __future__ import annotations

from typing import Any, ClassVar

from congclaw.skills.base import Skill, SkillContext, SkillParameter
from congclaw.skills.business_store import list_packages


class ListPackagesSkill(Skill):
    name: ClassVar[str] = "list_packages"
    description: ClassVar[str] = (
        "查询当前可办理的电信套餐目录（档位名称、月费、流量、语音与宽带权益）。"
        "用户问「有哪些套餐」「套餐档位」「199 档是什么套餐」"
        "「想换套餐有什么选择」时调用；可传 keyword 按档位关键字过滤。"
    )
    access: ClassVar[str] = "read"
    parameters: ClassVar[list[SkillParameter]] = [
        SkillParameter(
            name="keyword",
            description="套餐关键字（如档位「199」「融合」），为空则返回全部可办理套餐",
            type="string",
            required=False,
        ),
    ]

    async def run(self, context: SkillContext, **kwargs: Any) -> dict[str, Any]:
        args = self.validate_arguments(kwargs, context)
        keyword = str(args.get("keyword") or "")
        packages = await list_packages(keyword)
        return {"query": "package_catalog", "keyword": keyword, "packages": packages, "count": len(packages)}


SKILL = ListPackagesSkill()
