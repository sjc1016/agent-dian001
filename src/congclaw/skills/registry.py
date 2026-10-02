"""P4-9 / P4-10：Skill 动态注册中心 + 目录监听热加载。

- 启动时扫描内置目录 ``skills/catalog/`` 与外部目录（默认 ``data/skills/``，
  可用环境变量 ``SKILLS_EXTRA_DIR`` 覆盖）；
- 后台守护线程按 ``SKILL_WATCH_INTERVAL``（默认 2s）轮询文件 mtime：
  新增文件 → 加载，修改文件 → 重载，删除文件 → 注销，全程无需重启服务；
- 文件模块通过 importlib 按"路径+mtime"唯一名加载，修改后重载不命中模块缓存；
- 线程安全：watcher 线程与 asyncio 事件循环并发访问，全部通过 RLock 串行化。

热插拔 Skill 文件约定：模块级暴露 ``SKILL``（Skill 实例）或 ``create_skill()``
工厂；文件名形如 ``my_skill.py``（小写字母/数字/下划线）。
"""

from __future__ import annotations

import importlib.util
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from congclaw.core.paths import find_project_root
from congclaw.skills.base import Skill

BUILTIN_CATALOG_DIR = Path(__file__).parent / "catalog"
DEFAULT_EXTRA_DIR = Path("data") / "skills"
_SAFE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(frozen=True)
class LoadedSkill:
    skill: Skill
    path: Path
    source: str  # builtin / external
    mtime_ns: int


class SkillRegistry:
    """扫描目录、动态加载并按名取用 Skill 的注册中心。"""

    def __init__(
        self,
        *,
        builtin_dir: Path | None = None,
        extra_dir: Path | None = None,
        watch: bool = False,
        poll_interval: float | None = None,
    ) -> None:
        load_dotenv()
        self._dirs: list[tuple[Path, str]] = [
            (Path(builtin_dir or BUILTIN_CATALOG_DIR), "builtin"),
            (
                Path(extra_dir if extra_dir is not None else self._resolve_extra_dir()),
                "external",
            ),
        ]
        self._lock = threading.RLock()
        self._loaded: dict[str, LoadedSkill] = {}
        # 热卸载的内置文件：路径 -> 注销时的 mtime_ns（文件内容变更后自动恢复）
        self._disabled_paths: dict[str, int] = {}
        self._stop = threading.Event()
        self._watcher: threading.Thread | None = None
        self._poll_interval = (
            float(poll_interval)
            if poll_interval is not None
            else _env_float("SKILL_WATCH_INTERVAL", 2.0)
        )
        for directory, _source in self._dirs:
            directory.mkdir(parents=True, exist_ok=True)
        self.rescan()
        if watch:
            self.start_watching()

    @staticmethod
    def _resolve_extra_dir() -> Path:
        raw = os.getenv("SKILLS_EXTRA_DIR", "").strip()
        path = Path(raw) if raw else DEFAULT_EXTRA_DIR
        if not path.is_absolute():
            path = find_project_root() / path
        return path

    # ------------------------------------------------------------------
    # 扫描 / 加载
    # ------------------------------------------------------------------

    def rescan(self) -> dict[str, Any]:
        """全量比对目录与注册表，执行增/删/改同步，返回变更报告。"""
        loaded: list[str] = []
        reloaded: list[str] = []
        unloaded: list[str] = []
        errors: list[dict[str, str]] = []

        with self._lock:
            seen_paths: set[Path] = set()
            for directory, source in self._dirs:
                for path in sorted(directory.glob("*.py")):
                    if path.name == "__init__.py" or path.name.startswith("_"):
                        continue
                    if not _SAFE_NAME_RE.match(path.stem):
                        continue
                    seen_paths.add(path.resolve())

            existing_by_path = {item.path.resolve(): name for name, item in self._loaded.items()}

            # 文件删除：注销
            for path, name in list(existing_by_path.items()):
                if path not in seen_paths:
                    self._loaded.pop(name, None)
                    unloaded.append(name)

            # 新增 / 变更
            for directory, source in self._dirs:
                for path in sorted(directory.glob("*.py")):
                    resolved = path.resolve()
                    if resolved not in seen_paths:
                        continue
                    try:
                        mtime_ns = resolved.stat().st_mtime_ns
                    except OSError:
                        continue
                    disabled_mtime = self._disabled_paths.get(str(resolved))
                    if disabled_mtime is not None and disabled_mtime == mtime_ns:
                        # 被热卸载的内置文件且内容未变：继续保持注销
                        continue
                    current_name = existing_by_path.get(resolved)
                    previous = self._loaded.get(current_name) if current_name else None
                    if previous is not None and previous.mtime_ns == mtime_ns:
                        continue
                    try:
                        skill = self._load_file(resolved)
                    except Exception as exc:  # 单个 Skill 出错不拖垮整表
                        errors.append(
                            {"path": str(resolved), "error": f"{type(exc).__name__}: {exc}"}
                        )
                        continue
                    if skill.name in self._loaded and self._loaded[skill.name].path.resolve() != resolved:
                        # 同名冲突：内置优先，其余跳过
                        errors.append(
                            {
                                "path": str(resolved),
                                "error": f"skill 名 {skill.name!r} 与已加载文件"
                                f" {self._loaded[skill.name].path} 冲突，已跳过",
                            }
                        )
                        continue
                    self._disabled_paths.pop(str(resolved), None)
                    if previous is not None and previous.skill.name != skill.name:
                        self._loaded.pop(previous.skill.name, None)
                        unloaded.append(previous.skill.name)
                    self._loaded[skill.name] = LoadedSkill(
                        skill=skill, path=resolved, source=source, mtime_ns=mtime_ns
                    )
                    (reloaded if previous is not None else loaded).append(skill.name)

        report = {"loaded": loaded, "reloaded": reloaded, "unloaded": unloaded, "errors": errors}
        return report

    def _load_file(self, path: Path) -> Skill:
        unique_name = f"_mokio_skill_{path.stem}_{abs(hash((str(path), path.stat().st_mtime_ns)))}"
        spec = importlib.util.spec_from_file_location(unique_name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"无法为 {path} 创建模块 spec")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        candidate: Any = getattr(module, "SKILL", None)
        if candidate is None and hasattr(module, "create_skill"):
            candidate = module.create_skill()
        if not isinstance(candidate, Skill):
            raise AttributeError(
                f"{path.name} 必须暴露模块级 `SKILL`（Skill 实例）或 `create_skill()` 工厂"
            )
        return candidate

    # ------------------------------------------------------------------
    # 查询取用
    # ------------------------------------------------------------------

    def get(self, name: str) -> Skill:
        with self._lock:
            item = self._loaded.get(name)
        if item is None:
            raise KeyError(f"skill 未注册：{name}")
        return item.skill

    def try_get(self, name: str) -> Skill | None:
        with self._lock:
            item = self._loaded.get(name)
        return item.skill if item is not None else None

    def all(self) -> list[Skill]:
        with self._lock:
            return [item.skill for item in self._loaded.values()]

    def list_metadata(self) -> list[dict[str, Any]]:
        with self._lock:
            items = sorted(self._loaded.values(), key=lambda item: item.skill.name)
            return [
                {
                    **item.skill.metadata(),
                    "source": item.source,
                    "file": str(item.path),
                }
                for item in items
            ]

    def openai_tools(self) -> list[dict[str, Any]]:
        """思考节点 bind_tools 用的工具描述列表（注册即生效，天然支持热插拔）。"""
        return [skill.to_openai_tool() for skill in self.all()]

    # ------------------------------------------------------------------
    # 热注册 / 热卸载（管理接口）
    # ------------------------------------------------------------------

    def external_dir(self) -> Path:
        return self._dirs[1][0]

    def write_external_skill(self, name: str, content: str) -> dict[str, Any]:
        """把源码写入外部目录并立即重扫。文件名非法或加载失败时回滚删除。"""
        if not _SAFE_NAME_RE.match(name):
            raise ValueError("Skill 文件名只能包含小写字母、数字、下划线，且以字母开头")
        directory = self.external_dir()
        directory.mkdir(parents=True, exist_ok=True)
        path = (directory / f"{name}.py").resolve()
        path.write_text(content, encoding="utf-8")
        report = self.rescan()
        loaded_name = self._name_for_path(path)
        if not loaded_name:
            path.unlink(missing_ok=True)
            self.rescan()
            raise ValueError(f"Skill 文件写入但加载失败：{report['errors']}")
        return {"name": loaded_name, "file": str(path), "report": report}

    def _name_for_path(self, path: Path) -> str:
        """按文件路径反查已注册的 Skill 名（未加载返回空串）。"""
        resolved = path.resolve()
        with self._lock:
            for name, item in self._loaded.items():
                if item.path.resolve() == resolved:
                    return name
        return ""

    def unregister(self, name: str) -> dict[str, Any]:
        """按名热卸载：外部文件直接删除；内置文件仅注销（内容变更后自动恢复）。"""
        with self._lock:
            item = self._loaded.pop(name, None)
        if item is None:
            raise KeyError(f"skill 未注册：{name}")
        removed_file = False
        if item.source == "external":
            try:
                item.path.unlink(missing_ok=True)
                removed_file = True
            except OSError:
                pass
        else:
            with self._lock:
                self._disabled_paths[str(item.path.resolve())] = item.mtime_ns
        report = self.rescan()
        return {
            "name": name,
            "source": item.source,
            "file_removed": removed_file,
            "report": report,
        }

    # ------------------------------------------------------------------
    # 目录监听线程
    # ------------------------------------------------------------------

    def start_watching(self) -> None:
        if self._watcher is not None and self._watcher.is_alive():
            return
        self._stop.clear()
        self._watcher = threading.Thread(
            target=self._watch_loop, name="skill-registry-watcher", daemon=True
        )
        self._watcher.start()

    def stop_watching(self) -> None:
        self._stop.set()
        if self._watcher is not None:
            self._watcher.join(timeout=2.0)
        self._watcher = None

    def _watch_loop(self) -> None:
        while not self._stop.wait(self._poll_interval):
            try:
                self.rescan()
            except Exception:
                # 监听线程不能因单次扫描异常退出
                continue

    @property
    def watching(self) -> bool:
        return self._watcher is not None and self._watcher.is_alive()


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# 进程内单例（FastAPI lifespan 管理生命周期）
# ---------------------------------------------------------------------------

_registry: SkillRegistry | None = None
_registry_lock = threading.Lock()


def get_registry() -> SkillRegistry:
    """获取进程内共享注册中心（首次创建时扫描目录，默认开启目录监听）。"""
    global _registry
    with _registry_lock:
        if _registry is None:
            _registry = SkillRegistry(watch=os.getenv("SKILL_WATCH", "1") != "0")
        return _registry


def shutdown_registry() -> None:
    global _registry
    with _registry_lock:
        if _registry is not None:
            _registry.stop_watching()
        _registry = None
