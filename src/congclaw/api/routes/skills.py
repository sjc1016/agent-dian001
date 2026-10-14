"""P4-11：编排技能动态管理路由（热插拔编排能力的对外管理面）。

- ``GET    /api/v1/skills``：列出当前注册中心全部编排技能元信息（含内置/外置来源）；
- ``POST   /api/v1/skills``：上传一个 Skill 源码文件到外置目录（默认 data/skills/），
  写入后立即重扫，运行中进程无需重启即可被意图路由发现并挑选；
- ``DELETE /api/v1/skills/{name}``：热卸载（外置文件删除；内置文件仅注销，
  文件内容变更后由监听线程自动恢复）。

编排技能不进 LLM 的工具表，原子工具的注册与取用见 ``/api/v1/tools``。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from congclaw.skills.registry import get_skill_registry

router = APIRouter(prefix="/api/v1/skills", tags=["skills"])


class CreateSkillRequest(BaseModel):
    """热注册 Skill 请求体：name 为文件/调用名，content 为合法 Skill 源码。"""

    name: str = Field(..., description="Skill 文件名（小写字母/数字/下划线，字母开头，不带 .py）")
    content: str = Field(..., min_length=1, description="Skill Python 源码，模块级需定义 SKILL 实例或 create_skill()")


@router.get("")
async def list_skills() -> dict:
    """列出全部已加载编排技能的元信息（intent_router 挑选候选即取同一注册表）。"""
    registry = get_skill_registry()
    items = registry.list_metadata()
    builtin = [item for item in items if item.get("source") == "builtin"]
    external = [item for item in items if item.get("source") == "external"]
    return {
        "total": len(items),
        "builtin_count": len(builtin),
        "external_count": len(external),
        "skills": items,
    }


@router.post("", status_code=201)
async def create_skill(request: CreateSkillRequest) -> dict:
    """写入外置 Skill 文件并立即热注册；非法文件回滚并返回 400。"""
    registry = get_skill_registry()
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
async def delete_skill(name: str) -> dict:
    """热卸载 Skill：外置文件直接删除，内置 Skill 仅内存注销。"""
    registry = get_skill_registry()
    try:
        result = registry.unregister(name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"编排技能不存在：{name}") from exc
    return {"status": "unregistered", **result}
