"""原子工具 / 编排技能共用的热加载注册中心基座。

- 启动时扫描「内置目录 + 外置目录」，按文件 mtime 增量同步（新增 / 变更 / 删除）；
- 后台守护线程按 ``HOTLOAD_WATCH_INTERVAL``（默认 2s）轮询，运行中增删能力文件
  无需重启服务；
- 文件模块通过 importlib 按「路径 + mtime」唯一名加载，修改后重载不命中模块缓存；
- 线程安全：watcher 线程与 asyncio 事件循环并发访问，全部通过 RLock 串行化。

热插拔文件约定：模块级暴露 :attr:`symbol_name` 声明的对象（``TOOL`` 或 ``SKILL``），
或提供 :attr:`factory_name` 同名工厂；文件名形如 ``my_tool.py``（小写字母/数字/下划线）。

本模块只负责「目录 → 实例」的搬运，具体能力语义（工具进 LLM 工具表、技能由意图
路由挑选）由 :mod:`congclaw.tools.registry` 与 :mod:`congclaw.skills.registry` 决定。
"""

from __future__ import annotations

import importlib.util
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from dotenv import load_dotenv

from congclaw.core.paths import find_project_root

_SAFE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(frozen=True)
class LoadedItem:
    """一条已加载的能力记录（工具实例或编排技能实例）。"""

    item: Any
    path: Path
    source: str  # builtin / external
    mtime_ns: int


class HotLoadRegistry:
    """扫描目录、动态加载并按名取用能力的注册中心基座。"""

    #: 报错文案使用的中文名（如「工具」「编排技能」）
    item_label: ClassVar[str] = "能力"
    #: 热插拔文件必须暴露的模块级对象名（TOOL / SKILL）
    symbol_name: ClassVar[str] = ""
    #: 可选的模块级工厂函数名（create_tool / create_skill）
    factory_name: ClassVar[str] = ""
    #: 允许加载的实例类型，不符即报错，避免两类能力混装
    item_type: ClassVar[type] = object

    def __init__(
        self,
        *,
        builtin_dir: Path | None = None,
        extra_dir: Path | None = None,
        watch: bool = False,
        poll_interval: float | None = None,
    ) -> None:
        load_dotenv()
        builtin = Path(builtin_dir or self.builtin_dir())
        external = Path(extra_dir if extra_dir is not None else self._resolve_extra_dir())
        self._external_dir = external
        self._dirs: list[tuple[Path, str]] = [
            (builtin, "builtin"),
            (external, "external"),
        ]
        self._lock = threading.RLock()
        self._loaded: dict[str, LoadedItem] = {}
        # 热卸载的内置文件：路径 -> 注销时的 mtime_ns（文件内容变更后自动恢复）
        self._disabled_paths: dict[str, int] = {}
        self._stop = threading.Event()
        self._watcher: threading.Thread | None = None
        self._poll_interval = (
            float(poll_interval)
            if poll_interval is not None
            else _env_float("HOTLOAD_WATCH_INTERVAL", 2.0)
        )
        for directory, _source in self._dirs:
            directory.mkdir(parents=True, exist_ok=True)
        self.rescan()
        if watch:
            self.start_watching()

    # ------------------------------------------------------------------
    # 子类约定
    # ------------------------------------------------------------------

    @classmethod
    def builtin_dir(cls) -> Path:
        """内置目录（子类覆写）。"""
        raise NotImplementedError

    @classmethod
    def extra_dir_env(cls) -> str:
        """外置目录环境变量名（子类覆写）。"""
        raise NotImplementedError

    @classmethod
    def default_extra_dir(cls) -> Path:
        """外置目录默认相对路径（子类覆写）。"""
        raise NotImplementedError

    @classmethod
    def _resolve_extra_dir(cls) -> Path:
        raw = os.getenv(cls.extra_dir_env(), "").strip()
        path = Path(raw) if raw else cls.default_extra_dir()
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
            for directory, _source in self._dirs:
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
                        item = self._load_file(resolved)
                    except Exception as exc:  # 单个能力文件出错不拖垮整表
                        errors.append(
                            {"path": str(resolved), "error": f"{type(exc).__name__}: {exc}"}
                        )
                        continue
                    if item.name in self._loaded and self._loaded[item.name].path.resolve() != resolved:
                        # 同名冲突：内置优先，其余跳过
                        errors.append(
                            {
                                "path": str(resolved),
                                "error": f"{self.item_label}名 {item.name!r} 与已加载文件"
                                f" {self._loaded[item.name].path} 冲突，已跳过",
                            }
                        )
                        continue
                    self._disabled_paths.pop(str(resolved), None)
                    if previous is not None and previous.item.name != item.name:
                        self._loaded.pop(previous.item.name, None)
                        unloaded.append(previous.item.name)
                    self._loaded[item.name] = LoadedItem(
                        item=item, path=resolved, source=source, mtime_ns=mtime_ns
                    )
                    (reloaded if previous is not None else loaded).append(item.name)

        return {"loaded": loaded, "reloaded": reloaded, "unloaded": unloaded, "errors": errors}

    def _load_file(self, path: Path) -> Any:
        unique_name = f"_cong_hotload_{path.stem}_{abs(hash((str(path), path.stat().st_mtime_ns)))}"
        spec = importlib.util.spec_from_file_location(unique_name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"无法为 {path} 创建模块 spec")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        candidate: Any = getattr(module, self.symbol_name, None)
        if candidate is None and self.factory_name and hasattr(module, self.factory_name):
            candidate = getattr(module, self.factory_name)()
        if not isinstance(candidate, self.item_type):
            raise AttributeError(
                f"{path.name} 必须暴露模块级 `{self.symbol_name}`（{self.item_type.__name__} 实例）"
                f"或 `{self.factory_name}()` 工厂"
            )
        return candidate

    # ------------------------------------------------------------------
    # 查询取用
    # ------------------------------------------------------------------

    def get(self, name: str) -> Any:
        with self._lock:
            item = self._loaded.get(name)
        if item is None:
            raise KeyError(f"{self.item_label}未注册：{name}")
        return item.item

    def try_get(self, name: str) -> Any | None:
        with self._lock:
            item = self._loaded.get(name)
        return item.item if item is not None else None

    def names(self) -> list[str]:
        with self._lock:
            return sorted(self._loaded)

    def all(self) -> list[Any]:
        with self._lock:
            return [item.item for item in self._loaded.values()]

    def list_metadata(self) -> list[dict[str, Any]]:
        with self._lock:
            items = sorted(self._loaded.values(), key=lambda item: item.item.name)
            return [
                {**item.item.metadata(), "source": item.source, "file": str(item.path)}
                for item in items
            ]

    # ------------------------------------------------------------------
    # 热注册 / 热卸载（管理接口）
    # ------------------------------------------------------------------

    def external_dir(self) -> Path:
        return self._external_dir

    def write_external_item(self, name: str, content: str) -> dict[str, Any]:
        """把源码写入外置目录并立即重扫。文件名非法或加载失败时回滚删除。"""
        if not _SAFE_NAME_RE.match(name):
            raise ValueError(f"{self.item_label}文件名只能包含小写字母、数字、下划线，且以字母开头")
        directory = self.external_dir()
        directory.mkdir(parents=True, exist_ok=True)
        path = (directory / f"{name}.py").resolve()
        path.write_text(content, encoding="utf-8")
        report = self.rescan()
        loaded_name = self._name_for_path(path)
        if not loaded_name:
            path.unlink(missing_ok=True)
            self.rescan()
            raise ValueError(f"{self.item_label}文件写入但加载失败：{report['errors']}")
        return {"name": loaded_name, "file": str(path), "report": report}

    def _name_for_path(self, path: Path) -> str:
        """按文件路径反查已注册的能力名（未加载返回空串）。"""
        resolved = path.resolve()
        with self._lock:
            for name, item in self._loaded.items():
                if item.path.resolve() == resolved:
                    return name
        return ""

    def unregister(self, name: str) -> dict[str, Any]:
        """按名热卸载：外置文件直接删除；内置文件仅注销（内容变更后自动恢复）。"""
        with self._lock:
            item = self._loaded.pop(name, None)
        if item is None:
            raise KeyError(f"{self.item_label}未注册：{name}")
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
            target=self._watch_loop, name=f"{type(self).__name__}-watcher", daemon=True
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


def watch_enabled() -> bool:
    """目录监听总开关（``HOTLOAD_WATCH=0`` 关闭，默认开启）。"""
    load_dotenv()
    return os.getenv("HOTLOAD_WATCH", "1") != "0"


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


__all__ = ["HotLoadRegistry", "LoadedItem", "watch_enabled"]
