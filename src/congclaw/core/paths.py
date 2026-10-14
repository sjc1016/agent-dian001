from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from uuid import uuid4


def _find_marker_root(base: Path) -> Path | None:
    """从 ``base`` 向上查找最近的 ``pyproject.toml`` / ``.git`` 标记目录。"""
    base = base.parent if base.is_file() else base
    for candidate in (base, *base.parents):
        if (candidate / "pyproject.toml").exists() or (candidate / ".git").exists():
            return candidate
    return None


def find_project_root(start: Path | None = None) -> Path:
    """定位项目根目录。

    默认（``start`` 缺省）优先从本模块所在的源码树向上查找项目标记，
    这样无论从哪个工作目录启动（例如在 ``C:\\Users\\...`` 下运行后端），
    工作区与数据库都稳定落在项目目录内，不会跟着 ``cwd`` 落到 C 盘。
    显式传入 ``start`` 时按该路径向上查找，找不到标记时退回该路径本身。
    """
    if start is None:
        source_root = _find_marker_root(Path(__file__).resolve())
        if source_root is not None:
            return source_root
        start = Path.cwd()
    resolved = Path(start).resolve()
    return _find_marker_root(resolved) or resolved


def default_workspace(root: Path | None = None) -> Path:
    return new_task_workspace(root)


def default_workspace_root(root: Path | None = None) -> Path:
    return (root or find_project_root()) / ".congclaw" / "workspaces"


def new_task_workspace(root: Path | None = None) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = uuid4().hex[:6]
    return default_workspace_root(root) / f"workspace-{stamp}-{suffix}"


# 引入多用户隔离之前，会话直接落在 ``<root>/<dir>/workspaces/workspace-*`` 下，
# 没有 ``user-<phone>`` 归属目录。``.mokioclaw`` 是更早一代的目录命名。
LEGACY_WORKSPACE_DIRS = (".congclaw", ".mokioclaw")

# 多用户工作区约定：``.../workspaces/user-<phone>/session-<时间戳>-<随机>``
_USER_WORKSPACE_PATTERN = re.compile(r"user-([0-9A-Za-z]+)")


def phone_from_workspace(workspace: str | Path) -> str:
    """从工作区路径中提取归属用户号码（``user-<phone>`` 目录段）。

    返回空串表示该工作区没有 ``user-<phone>`` 归属（多用户改造前的历史工作区，
    或未绑定用户的临时工作区）。Windows 路径的反斜杠会先归一化为正斜杠。
    """
    normalized = str(workspace).replace("\\", "/")
    match = _USER_WORKSPACE_PATTERN.search(normalized)
    return match.group(1) if match else ""


def legacy_workspace_prefixes(root: Path | None = None) -> list[str]:
    """返回「未归属用户」的历史工作区路径前缀。

    注意这里返回的是**叶子目录名前缀**（形如 ``.../workspaces/workspace-``），
    而不是某个父目录的路径前缀——遗留工作区本身就叫 ``workspace-<时间戳>-<随机>``，
    因此调用方不要再补路径分隔符。

    这些工作区创建于多用户改造之前，无法从路径反推归属用户，因此统一视为
    默认演示用户（``DEFAULT_DEMO_PHONE``）的历史资产：默认用户查询会话时
    会额外带上这些前缀，使其历史记录不因引入用户维度而「消失」。
    """
    base = root or find_project_root()
    return [str(base / name / "workspaces" / "workspace-") for name in LEGACY_WORKSPACE_DIRS]
