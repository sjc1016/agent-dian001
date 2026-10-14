"""内置编排技能目录（四个首批技能）。

每个模块暴露模块级 ``SKILL`` 实例，由 :mod:`congclaw.skills.registry`
扫描本目录动态加载；向外置技能目录增删文件即可热插拔扩展。
"""

from __future__ import annotations
