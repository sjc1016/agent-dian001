"""P4-1：Skill 基类与参数约定。

每个 Skill 用声明式元信息（name / description / access / parameters）描述自己，
``to_openai_tool`` 直接产出 OpenAI function-calling 的 JSON Schema，供思考节点
``model.bind_tools([...])`` 使用；运行时统一走 :meth:`Skill.run`（原生 async）。

热插拔文件（catalog/ 与外部目录）只需在模块级暴露 ``SKILL`` 实例或
``create_skill()`` 工厂，注册中心即可动态加载（见 :mod:`mokioclaw.skills.registry`）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar


class SkillError(Exception):
    """Skill 执行业务失败（账户不存在、参数非法、办理异常等）。

    编排节点捕获后以 ``{"ok": False, "error": ...}`` 回灌反思节点，不中断整图。
    """

    def __init__(self, message: str, *, code: str = "skill_error") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class SkillParameter:
    """单个入参声明。type 取 string/integer/number/boolean/array。"""

    name: str
    description: str
    type: str = "string"
    required: bool = False
    enum: list[Any] | None = None
    default: Any = None


@dataclass(frozen=True)
class SkillContext:
    """一次会话/一次调用内的公共上下文（不显式暴露给 LLM 作为工具参数）。"""

    phone: str  # 当前会话的默认用户号码
    workspace: str = ""  # 会话工作区（pending approval 按会话隔离用）
    approval_mode: str = "inline"  # inline / auto / deny（复用 core.approval 语义）


class Skill:
    """所有电信业务 Skill 的基类。子类设置类属性并实现 :meth:`run`。"""

    #: 工具唯一名（snake_case，与 LLM function name 一致）
    name: ClassVar[str] = ""
    #: 给 LLM 看的功能描述（决定何时调用本工具）
    description: ClassVar[str] = ""
    #: read=只读；write=写操作（触发反思校验，必要时人工确认）
    access: ClassVar[str] = "read"
    #: 写操作是否必须人工确认后执行（P4-16，仅 access="write" 时生效）
    requires_confirmation: ClassVar[bool] = False

    parameters: ClassVar[list[SkillParameter]] = []

    def __init__(self) -> None:
        if not self.name:
            raise SkillError(f"{type(self).__name__} 缺少 name 声明")
        if self.access not in ("read", "write"):
            raise SkillError(f"{self.name} 的 access 只能是 read/write")

    # ------------------------------------------------------------------
    # 元信息 / schema
    # ------------------------------------------------------------------

    def metadata(self) -> dict[str, Any]:
        """注册中心与管理接口使用的可 JSON 化元信息。"""
        return {
            "name": self.name,
            "description": self.description,
            "access": self.access,
            "requires_confirmation": bool(self.requires_confirmation),
            "parameters": [
                {
                    "name": param.name,
                    "type": param.type,
                    "description": param.description,
                    "required": bool(param.required),
                    **({"enum": param.enum} if param.enum is not None else {}),
                    **(
                        {"default": param.default}
                        if param.required is False and param.default is not None
                        else {}
                    ),
                }
                for param in self.parameters
            ],
        }

    def to_openai_tool(self) -> dict[str, Any]:
        """产出 OpenAI function-calling 工具描述（LangChain bind_tools 接受 dict 形式）。"""
        properties: dict[str, Any] = {}
        required_names: list[str] = []
        for param in self.parameters:
            spec: dict[str, Any] = {
                "type": param.type if param.type != "array" else "array",
                "description": param.description,
            }
            if param.type == "array":
                spec["items"] = {"type": "string"}
            if param.enum is not None:
                spec["enum"] = list(param.enum)
            properties[param.name] = spec
            if param.required:
                required_names.append(param.name)
        schema: dict[str, Any] = {"type": "object", "properties": properties}
        if required_names:
            schema["required"] = required_names
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description, "parameters": schema},
        }

    # ------------------------------------------------------------------
    # 参数校验与执行
    # ------------------------------------------------------------------

    def validate_arguments(self, raw: dict[str, Any] | None, context: SkillContext) -> dict[str, Any]:
        """按声明校验/强转入参：必填检查、类型转换、enum 检查、默认值填充。

        LLM 偶尔会把数字写成字符串、漏填可选项，这里统一收敛，避免每个 Skill 重复处理。
        """
        raw = dict(raw or {})
        cleaned: dict[str, Any] = {}
        declared = {param.name: param for param in self.parameters}

        unexpected = sorted(set(raw) - set(declared))
        if unexpected:
            raise SkillError(f"{self.name} 收到未声明参数：{', '.join(unexpected)}")

        for pname, param in declared.items():
            value = raw.get(pname, param.default)
            if value is None or (isinstance(value, str) and not value.strip()):
                if param.required:
                    raise SkillError(f"{self.name} 缺少必填参数：{pname}")
                continue
            cleaned[pname] = self._coerce(param, value)
        return cleaned

    @staticmethod
    def _coerce(param: SkillParameter, value: Any) -> Any:
        kind = param.type
        try:
            if kind == "string":
                coerced = str(value).strip()
            elif kind == "integer":
                coerced = int(value) if not isinstance(value, str) else int(float(value.strip()))
            elif kind == "number":
                coerced = float(value)
            elif kind == "boolean":
                if isinstance(value, bool):
                    coerced = value
                else:
                    coerced = str(value).strip().lower() in ("true", "1", "yes", "y", "是", "确认")
            elif kind == "array":
                coerced = list(value) if isinstance(value, (list, tuple)) else [str(value)]
                coerced = [str(item).strip() for item in coerced if str(item).strip()]
            else:
                coerced = value
        except (TypeError, ValueError) as exc:
            raise SkillError(f"参数 {param.name} 类型应为 {kind}，实际值：{value!r}") from exc

        if kind == "string" and not coerced and param.required:
            raise SkillError(f"{param.name} 不能为空")
        if param.enum is not None and coerced not in param.enum:
            raise SkillError(
                f"参数 {param.name} 只支持 {param.enum}，实际值：{coerced!r}",
                code="invalid_enum",
            )
        return coerced

    async def run(self, context: SkillContext, **kwargs: Any) -> dict[str, Any]:
        """业务执行入口，子类必须实现。

        成功返回可 JSON 化的业务数据 dict；失败抛 :class:`SkillError`，
        由编排节点统一转换为失败结果交反思节点决策。
        """
        raise NotImplementedError
