"""阶段 4 验收（P4-9/P4-10 + 验收门④）：Skill 动态注册中心。

覆盖：运行中写入即可发现调用、删除即注销、内置 Skill 热卸载后内容变更自动恢复、
非法文件名忽略、同名冲突内置优先、后台 watcher 线程免重启热加载。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from congclaw.skills.base import SkillContext
from congclaw.skills.registry import SkillRegistry

_ECHO_SKILL_SOURCE = '''
from congclaw.skills.base import Skill, SkillParameter


class EchoSkill(Skill):
    name = "echo_demo"
    description = "echo back the input text"
    parameters = [SkillParameter(name="text", description="text to echo", required=True)]

    async def run(self, context, **kwargs):
        return {"echo": kwargs.get("text")}


SKILL = EchoSkill()
'''


def _make_dirs(tmp_path: Path) -> tuple[Path, Path]:
    builtin = tmp_path / "builtin"
    external = tmp_path / "external"
    builtin.mkdir()
    external.mkdir()
    return builtin, external


def _run(coro):
    return asyncio.run(coro)


def test_empty_registry_starts_clean(tmp_path: Path) -> None:
    builtin, external = _make_dirs(tmp_path)
    registry = SkillRegistry(builtin_dir=builtin, extra_dir=external, watch=False)

    assert registry.all() == []
    assert registry.try_get("echo_demo") is None
    assert registry.openai_tools() == []


def test_external_file_discovered_after_rescan(tmp_path: Path) -> None:
    builtin, external = _make_dirs(tmp_path)
    registry = SkillRegistry(builtin_dir=builtin, extra_dir=external, watch=False)

    (external / "echo_demo.py").write_text(_ECHO_SKILL_SOURCE, encoding="utf-8")
    report = registry.rescan()

    assert "echo_demo" in report["loaded"]
    skill = registry.try_get("echo_demo")
    assert skill is not None
    result = _run(skill.run(SkillContext(phone="13800138000"), text="你好"))
    assert result == {"echo": "你好"}


def test_write_external_skill_takes_effect_immediately(tmp_path: Path) -> None:
    """验收门④：运行中通过管理接口新增 Skill，无需重启当轮即可调用。"""
    builtin, external = _make_dirs(tmp_path)
    registry = SkillRegistry(builtin_dir=builtin, extra_dir=external, watch=False)

    info = registry.write_external_skill("echo_demo", _ECHO_SKILL_SOURCE)

    assert info["name"] == "echo_demo"
    assert Path(info["file"]).exists()
    assert registry.get("echo_demo").name == "echo_demo"
    metadata = {item["name"]: item for item in registry.list_metadata()}
    assert metadata["echo_demo"]["source"] == "external"


def test_write_external_skill_rejects_bad_name_and_rolls_back(tmp_path: Path) -> None:
    builtin, external = _make_dirs(tmp_path)
    registry = SkillRegistry(builtin_dir=builtin, extra_dir=external, watch=False)

    with pytest.raises(ValueError):
        registry.write_external_skill("bad-name", _ECHO_SKILL_SOURCE)

    # 内容无法加载（缺少 SKILL 暴露）：报错并回滚删除文件
    with pytest.raises(ValueError):
        registry.write_external_skill("broken_skill", "x = 1\n")
    assert not (external / "broken_skill.py").exists()
    assert registry.try_get("broken_skill") is None


def test_unregister_external_deletes_file_and_unloads(tmp_path: Path) -> None:
    builtin, external = _make_dirs(tmp_path)
    registry = SkillRegistry(builtin_dir=builtin, extra_dir=external, watch=False)
    registry.write_external_skill("echo_demo", _ECHO_SKILL_SOURCE)

    info = registry.unregister("echo_demo")

    assert info["source"] == "external"
    assert info["file_removed"] is True
    assert not (external / "echo_demo.py").exists()
    assert registry.try_get("echo_demo") is None
    with pytest.raises(KeyError):
        registry.unregister("echo_demo")


def test_builtin_unregister_then_auto_recover_on_change(tmp_path: Path) -> None:
    """内置 Skill 热卸载只注销不删文件；文件内容变更（mtime 变化）后自动恢复。"""
    builtin, external = _make_dirs(tmp_path)
    skill_file = builtin / "echo_demo.py"
    skill_file.write_text(_ECHO_SKILL_SOURCE, encoding="utf-8")
    registry = SkillRegistry(builtin_dir=builtin, extra_dir=external, watch=False)
    assert registry.try_get("echo_demo") is not None

    info = registry.unregister("echo_demo")

    assert info["source"] == "builtin"
    assert info["file_removed"] is False
    assert skill_file.exists()  # 内置文件不删除
    assert registry.try_get("echo_demo") is None

    # 内容未变：重扫仍保持注销
    registry.rescan()
    assert registry.try_get("echo_demo") is None

    # 内容变更（mtime_ns 变化）：自动恢复注册
    time.sleep(0.01)
    skill_file.write_text(_ECHO_SKILL_SOURCE + "\n# changed\n", encoding="utf-8")
    report = registry.rescan()
    assert "echo_demo" in report["loaded"]
    assert registry.try_get("echo_demo") is not None


def test_modified_external_skill_hot_reloads(tmp_path: Path) -> None:
    builtin, external = _make_dirs(tmp_path)
    registry = SkillRegistry(builtin_dir=builtin, extra_dir=external, watch=False)
    registry.write_external_skill("echo_demo", _ECHO_SKILL_SOURCE)

    time.sleep(0.01)
    renamed_source = _ECHO_SKILL_SOURCE.replace('name = "echo_demo"', 'name = "echo_v2"')
    (external / "echo_demo.py").write_text(renamed_source, encoding="utf-8")
    report = registry.rescan()

    # 同一文件变更：新名进 reloaded，旧名进 unloaded
    assert "echo_v2" in report["reloaded"]
    assert "echo_demo" in report["unloaded"]
    assert registry.try_get("echo_demo") is None
    assert registry.try_get("echo_v2") is not None


def test_unsafe_filenames_ignored(tmp_path: Path) -> None:
    builtin, external = _make_dirs(tmp_path)
    registry = SkillRegistry(builtin_dir=builtin, extra_dir=external, watch=False)

    (external / "bad-name.py").write_text(_ECHO_SKILL_SOURCE, encoding="utf-8")
    (external / "1start.py").write_text(_ECHO_SKILL_SOURCE, encoding="utf-8")
    (external / "_private.py").write_text(_ECHO_SKILL_SOURCE, encoding="utf-8")
    registry.rescan()

    assert registry.all() == []


def test_builtin_takes_precedence_on_name_conflict(tmp_path: Path) -> None:
    """启动扫描时内置/外部同名：内置胜出，外部文件跳过并记录冲突。"""
    builtin, external = _make_dirs(tmp_path)
    (builtin / "echo_demo.py").write_text(_ECHO_SKILL_SOURCE, encoding="utf-8")
    (external / "echo_demo.py").write_text(_ECHO_SKILL_SOURCE, encoding="utf-8")

    registry = SkillRegistry(builtin_dir=builtin, extra_dir=external, watch=False)

    assert registry.try_get("echo_demo") is not None
    source = {item["name"]: item["source"] for item in registry.list_metadata()}
    assert source["echo_demo"] == "builtin"


def test_watcher_thread_discovers_new_file_without_restart(tmp_path: Path) -> None:
    """验收门④：watcher 线程轮询，落盘即被发现（模拟不经过管理接口的直接投放）。"""
    builtin, external = _make_dirs(tmp_path)
    registry = SkillRegistry(
        builtin_dir=builtin, extra_dir=external, watch=True, poll_interval=0.05
    )
    try:
        assert registry.watching is True
        (external / "echo_demo.py").write_text(_ECHO_SKILL_SOURCE, encoding="utf-8")

        deadline = time.time() + 3.0
        while time.time() < deadline and registry.try_get("echo_demo") is None:
            time.sleep(0.05)

        assert registry.try_get("echo_demo") is not None
    finally:
        registry.stop_watching()
    assert registry.watching is False
