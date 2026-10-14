"""套餐咨询编排：按 ``scope`` 条件分支，查「我的套餐」或「可办理套餐目录」。"""

from __future__ import annotations

from typing import Any, ClassVar

from congclaw.skills.base import Skill, SkillContext
from congclaw.tools.base import ToolParameter

SCOPES = ("current", "catalog")

# 命中即判定为「查可办理的套餐目录」；其余一律按「查我的当前套餐」处理。
# 意图识别抽槽位时可能把半句自然语言（如「当前套餐及流量余量」）填进 scope，
# 这里做归一而不是让参数校验抛错——否则一次抽取失误就会让整轮编排作废。
_CATALOG_WORDS = (
    "catalog",
    "目录",
    "有哪些",
    "可以办",
    "可办",
    "可选",
    "全部套餐",
    "所有套餐",
    "套餐列表",
    "套餐档位",
)


def normalize_scope(raw: str, keyword: str = "") -> str:
    """把模型抽取的 scope 收敛到 current / catalog（默认 current）。"""
    text = f"{raw} {keyword}".strip().lower()
    if any(word in text for word in _CATALOG_WORDS):
        return "catalog"
    return "current"


class PackageQuerySkill(Skill):
    name: ClassVar[str] = "package_query"
    description: ClassVar[str] = "查询当前在用套餐与余量，或可办理的套餐目录"
    intent_hint: ClassVar[str] = (
        "用户问「我现在用的什么套餐」「流量还剩多少」「套餐余量」"
        "「有哪些套餐可以办」「199 档是什么套餐」等与套餐有关、需要账户数据的问题"
    )
    access: ClassVar[str] = "read"
    parameters: ClassVar[list[ToolParameter]] = [
        ToolParameter(
            name="scope",
            description=(
                "查询范围：current=我当前的套餐与余量；catalog=当前可办理的套餐目录"
            ),
            type="string",
            required=False,
            enum=list(SCOPES),
            default="current",
        ),
        ToolParameter(
            name="keyword",
            description="套餐关键字（如档位「199」「融合」），仅在 scope=catalog 时生效",
            type="string",
            required=False,
        ),
        ToolParameter(name="phone", description="要查询的手机号码", type="string", required=False),
    ]

    async def run(self, context: SkillContext, **kwargs: Any) -> dict[str, Any]:
        # 先归一 scope 再校验：模型抽出的自然语言不会因 enum 校验失败而作废整轮
        kwargs = dict(kwargs)
        kwargs["scope"] = normalize_scope(
            str(kwargs.get("scope") or ""), str(kwargs.get("keyword") or "")
        )
        args = self.validate_arguments(kwargs, context)
        scope = str(args.get("scope") or "current")
        phone = str(args.get("phone") or "")
        keyword = str(args.get("keyword") or "")

        if scope == "catalog":
            ok, catalog = await self.try_call_tool(context, "list_packages", keyword=keyword)
            steps = [self._step("list_packages", ok, catalog)]
            if not ok:
                return self._failed(
                    steps,
                    str(catalog.get("error") or ""),
                    str(catalog.get("error_code") or "tool_error"),
                )
            return self._finished(steps, {"scope": "catalog", "catalog": catalog})

        ok, package = await self.try_call_tool(context, "query_package", phone=phone)
        steps = [self._step("query_package", ok, package)]
        if not ok:
            return self._failed(
                steps,
                str(package.get("error") or ""),
                str(package.get("error_code") or "tool_error"),
            )
        return self._finished(steps, {"scope": "current", "package": package})


SKILL = PackageQuerySkill()
