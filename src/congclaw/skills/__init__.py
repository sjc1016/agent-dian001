"""编排技能层（Skill）。

- :class:`congclaw.skills.base.Skill` 定义编排技能基类（控制流 + 参数 schema + async run）；
- :mod:`congclaw.skills.catalog` 为内置四个编排技能；
- :mod:`congclaw.skills.registry` 提供动态注册与目录热插拔。

编排技能用代码确定性调用原子工具（:mod:`congclaw.tools`），不进 LLM 的工具表，
由主图意图路由挑选。业务数据仓储见 :mod:`congclaw.domain.business_store`。
"""

from __future__ import annotations

from congclaw.skills.base import ConfirmationRequired, Skill, SkillContext

__all__ = ["ConfirmationRequired", "Skill", "SkillContext"]
