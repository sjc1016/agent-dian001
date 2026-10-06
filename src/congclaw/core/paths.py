from __future__ import annotations

from datetime import datetime
from pathlib import Path
from uuid import uuid4


def find_project_root(start: Path | None = None) -> Path:
    """Find the nearest project root marker from ``start`` upward."""
    current = (start or Path.cwd()).resolve()
    if current.is_file():
        current = current.parent

    for candidate in (current, *current.parents):
        if (candidate / "pyproject.toml").exists() or (candidate / ".git").exists():
            return candidate
    return current


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
