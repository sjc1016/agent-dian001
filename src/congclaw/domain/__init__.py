"""电信客服领域数据层（模拟 BOSS/CRM 的 SQLite 异步仓储）。

本包只放业务数据访问与基线维护，不含编排逻辑（:mod:`congclaw.skills`）
与原子工具（:mod:`congclaw.tools`）。
"""

from __future__ import annotations
