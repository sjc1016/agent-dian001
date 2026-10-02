"""阶段 4：电信 Skill 工具集。

- :class:`congclaw.skills.base.Skill` 定义工具基类（元信息 + 参数 schema + async run）；
- :mod:`congclaw.skills.catalog` 为内置六个业务 Skill；
- :mod:`congclaw.skills.registry` 提供动态注册与目录热插拔；
- :mod:`congclaw.skills.business_store` 为模拟 BOSS/CRM 的 SQLite 异步仓储。
"""

from __future__ import annotations

from congclaw.skills.base import (
    Skill,
    SkillContext,
    SkillError,
    SkillParameter,
)

__all__ = ["Skill", "SkillContext", "SkillError", "SkillParameter"]
