"""编排技能动态注册中心 + 目录热加载。

- 内置目录 ``skills/catalog/``（四个编排技能）+ 外置目录（默认 ``data/skills/``，
  可用环境变量 ``SKILLS_EXTRA_DIR`` 覆盖），扫描/监听/热插拔逻辑复用
  :class:`congclaw.core.hotload.HotLoadRegistry`；
- 热插拔文件约定：模块级暴露 ``SKILL``（:class:`~congclaw.skills.base.Skill` 实例）
  或 ``create_skill()`` 工厂。

编排技能不进 LLM 的 ``bind_tools`` 工具表，只由主图 ``intent_router`` 依据
``intent_hint`` 挑选；原子工具的注册与取用见 :mod:`congclaw.tools.registry`。
"""

from __future__ import annotations

import threading
from pathlib import Path

from congclaw.core.hotload import HotLoadRegistry, watch_enabled
from congclaw.skills.base import Skill

BUILTIN_CATALOG_DIR = Path(__file__).parent / "catalog"
DEFAULT_EXTRA_DIR = Path("data") / "skills"


class SkillRegistry(HotLoadRegistry):
    """扫描目录、动态加载并按名取用编排技能的注册中心。"""

    item_label = "编排技能"
    symbol_name = "SKILL"
    factory_name = "create_skill"
    item_type = Skill

    @classmethod
    def builtin_dir(cls) -> Path:
        return BUILTIN_CATALOG_DIR

    @classmethod
    def extra_dir_env(cls) -> str:
        return "SKILLS_EXTRA_DIR"

    @classmethod
    def default_extra_dir(cls) -> Path:
        return DEFAULT_EXTRA_DIR


# ---------------------------------------------------------------------------
# 进程内单例（FastAPI lifespan 管理生命周期）
# ---------------------------------------------------------------------------

_registry: SkillRegistry | None = None
_registry_lock = threading.Lock()


def get_skill_registry() -> SkillRegistry:
    """获取进程内共享编排技能注册中心（首次创建时扫描目录，默认开启目录监听）。"""
    global _registry
    with _registry_lock:
        if _registry is None:
            _registry = SkillRegistry(watch=watch_enabled())
        return _registry


def shutdown_skill_registry() -> None:
    global _registry
    with _registry_lock:
        if _registry is not None:
            _registry.stop_watching()
        _registry = None


__all__ = [
    "BUILTIN_CATALOG_DIR",
    "DEFAULT_EXTRA_DIR",
    "SkillRegistry",
    "get_skill_registry",
    "shutdown_skill_registry",
]
