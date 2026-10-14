"""P4-11：原子工具动态管理路由（热插拔工具能力的对外管理面）。

- ``GET    /api/v1/tools``：列出当前注册中心全部工具元信息（含内置/外置来源），
  并附带思考节点 ``bind_tools`` 实际使用的 OpenAI 工具描述；
- ``POST   /api/v1/tools``：上传一个 Tool 源码文件到外置目录（默认 data/tools/），
  写入后立即重扫，运行中进程无需重启即可被模型当轮发现并调用；
- ``DELETE /api/v1/tools/{name}``：热卸载（外置文件删除；内置文件仅注销，
  文件内容变更后由监听线程自动恢复）。

原子工具是 LLM 唯一可直接调用的能力，编排技能的注册与取用见 ``/api/v1/skills``。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from congclaw.tools.registry import get_tool_registry

router = APIRouter(prefix="/api/v1/tools", tags=["tools"])


class CreateToolRequest(BaseModel):
    """热注册 Tool 请求体：name 为文件/调用名，content 为合法 Tool 源码。"""

    name: str = Field(..., description="工具文件名（小写字母/数字/下划线，字母开头，不带 .py）")
    content: str = Field(..., min_length=1, description="工具 Python 源码，模块级需定义 TOOL 实例或 create_tool()")


@router.get("")
async def list_tools() -> dict:
    """列出全部已加载工具的元信息（思考节点 bind_tools 即取同一注册表）。"""
    registry = get_tool_registry()
    items = registry.list_metadata()
    builtin = [item for item in items if item.get("source") == "builtin"]
    external = [item for item in items if item.get("source") == "external"]
    return {
        "total": len(items),
        "builtin_count": len(builtin),
        "external_count": len(external),
        "tools": items,
        "openai_tools": registry.openai_tools(),
    }


@router.post("", status_code=201)
async def create_tool(request: CreateToolRequest) -> dict:
    """写入外置 Tool 文件并立即热注册；非法文件回滚并返回 400。"""
    registry = get_tool_registry()
    try:
        result = registry.write_external_item(request.name.strip(), request.content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "status": "loaded",
        "name": result["name"],
        "file": result["file"],
        "report": result["report"],
    }


@router.delete("/{name}")
async def delete_tool(name: str) -> dict:
    """热卸载工具：外置文件直接删除，内置工具仅内存注销。"""
    registry = get_tool_registry()
    try:
        result = registry.unregister(name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"工具不存在：{name}") from exc
    return {"status": "unregistered", **result}
