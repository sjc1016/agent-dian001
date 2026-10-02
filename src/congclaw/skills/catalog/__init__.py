"""内置电信业务 Skill 目录（六个首批 Skill）。

每个模块暴露模块级 ``SKILL`` 实例，由 :mod:`congclaw.skills.registry`
扫描本目录动态加载；向外部 Skill 目录增删文件即可热插拔扩展。
"""

from __future__ import annotations
