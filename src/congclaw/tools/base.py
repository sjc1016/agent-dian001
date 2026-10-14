"""原子工具基类与参数约定。

原子工具（Tool）是 LLM 唯一可直接调用的能力：用声明式元信息
（name / description / access / parameters）描述自己，``to_openai_tool`` 直接产出
OpenAI function-calling 的 JSON Schema，供思考节点 ``model.bind_tools([...])`` 使用；
运行时统一走 :meth:`Tool.run`（原生 async）。

与编排技能（:mod:`congclaw.skills`）的分工：
- **工具**：单点业务动作，由模型自主选择调用，注册进 LLM 工具表；
- **技能**：代码确定性编排多个工具，不进 LLM 工具表，由意图路由挑选。

热插拔文件（catalog/ 与外置目录）只需在模块级暴露 ``TOOL`` 实例或 ``create_tool()``
工厂，注册中心即可动态加载（见 :mod:`congclaw.tools.registry`）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar


class ToolError(Exception):
    """工具执行业务失败（账户不存在、参数非法、办理异常等）。

    编排节点捕获后以 ``{"ok": False, "error": ...}`` 回灌反思节点，不中断整图。
    """

    def __init__(self, message: str, *, code: str = "tool_error") -> None:
        super().__init__(message)
        self.code = code


class ConfirmationRequired(Exception):
    """工具命中「需人工确认的写操作」时抛出。

    编排技能在执行写工具前中止，由执行节点落 pending approval 并返回确认卡片；
    用户下一轮回复「确认」后走既有的审批恢复路径执行，不再重跑整条编排。
    """

    def __init__(self, tool_name: str, args: dict[str, Any], summary: str = "") -> None:
        super().__init__(f"{tool_name} 需人工确认后执行")
        self.tool_name = tool_name
        # 注意：属性名不能叫 args —— 那是 BaseException 的保留属性（赋值会被强转为元组）
        self.tool_args = dict(args)
        self.summary = summary


@dataclass(frozen=True)
class ToolParameter:
    """工具入参声明。type 取 string/integer/number/boolean/array。"""

    name: str
    description: str
    type: str = "string"
    required: bool = False
    enum: list[Any] | None = None
    default: Any = None


@dataclass(frozen=True)
class ToolContext:
    """一次调用内的公共上下文（不显式暴露给 LLM 作为工具参数）。"""

    phone: str  # 当前会话的默认用户号码
    workspace: str = ""  # 会话工作区（pending approval 按会话隔离用）
    approval_mode: str = "inline"  # inline / auto / deny（复用 core.approval 语义）


def validate_parameters(
    subject: str, parameters: list[ToolParameter], raw: dict[str, Any] | None
) -> dict[str, Any]:
    """按声明校验/强转入参：必填检查、类型转换、enum 检查、默认值填充。

    LLM 偶尔会把数字写成字符串、漏填可选项，这里统一收敛，避免每个工具重复处理。
    ``subject`` 只用于报错文案（工具名 / 技能名）。
    """
    raw = dict(raw or {})
    cleaned: dict[str, Any] = {}
    declared = {param.name: param for param in parameters}

    unexpected = sorted(set(raw) - set(declared))
    if unexpected:
        raise ToolError(f"{subject} 收到未声明参数：{', '.join(unexpected)}")

    for pname, param in declared.items():
        value = raw.get(pname, param.default)
        if value is None or (isinstance(value, str) and not value.strip()):
            if param.required:
                raise ToolError(f"{subject} 缺少必填参数：{pname}")
            continue
        cleaned[pname] = coerce_value(subject, param, value)
    return cleaned


def coerce_value(subject: str, param: ToolParameter, value: Any) -> Any:
    """把单个入参强转到声明类型，并校验 enum/非空约束。"""
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
        raise ToolError(f"{subject} 参数 {param.name} 类型应为 {kind}，实际值：{value!r}") from exc

    if kind == "string" and not coerced and param.required:
        raise ToolError(f"{subject} 参数 {param.name} 不能为空")
    if param.enum is not None and coerced not in param.enum:
        raise ToolError(
            f"{subject} 参数 {param.name} 只支持 {param.enum}，实际值：{coerced!r}",
            code="invalid_enum",
        )
    return coerced


def parameters_metadata(parameters: list[ToolParameter]) -> list[dict[str, Any]]:
    """把参数声明转成可 JSON 化的元信息列表。"""
    return [
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
        for param in parameters
    ]


def openai_tool_schema(
    name: str, description: str, parameters: list[ToolParameter]
) -> dict[str, Any]:
    """产出 OpenAI function-calling 工具描述（LangChain bind_tools 接受 dict 形式）。"""
    properties: dict[str, Any] = {}
    required_names: list[str] = []
    for param in parameters:
        spec: dict[str, Any] = {"type": param.type, "description": param.description}
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
        "function": {"name": name, "description": description, "parameters": schema},
    }


class Tool:
    """所有原子工具的基类。子类设置类属性并实现 :meth:`run`。"""

    #: 工具唯一名（snake_case，与 LLM function name 一致）
    name: ClassVar[str] = ""
    #: 给 LLM 看的功能描述（决定何时调用本工具）
    description: ClassVar[str] = ""
    #: read=只读；write=写操作（触发反思校验，必要时人工确认）
    access: ClassVar[str] = "read"
    #: 写操作是否必须人工确认后执行（P4-16，仅 access="write" 时生效）
    requires_confirmation: ClassVar[bool] = False

    parameters: ClassVar[list[ToolParameter]] = []

    def __init__(self) -> None:
        if not self.name:
            raise ToolError(f"{type(self).__name__} 缺少 name 声明")
        if self.access not in ("read", "write"):
            raise ToolError(f"{self.name} 的 access 只能是 read/write")

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
            "parameters": parameters_metadata(self.parameters),
        }

    def to_openai_tool(self) -> dict[str, Any]:
        return openai_tool_schema(self.name, self.description, self.parameters)

    # ------------------------------------------------------------------
    # 参数校验与执行
    # ------------------------------------------------------------------

    def validate_arguments(
        self, raw: dict[str, Any] | None, context: ToolContext | None = None
    ) -> dict[str, Any]:
        """按声明校验/强转入参（``context`` 仅为签名对齐，工具不使用会话上下文）。"""
        del context
        return validate_parameters(self.name, self.parameters, raw)

    async def run(self, context: ToolContext, **kwargs: Any) -> dict[str, Any]:
        """业务执行入口，子类必须实现。

        成功返回可 JSON 化的业务数据 dict；失败抛 :class:`ToolError`，
        由编排节点统一转换为失败结果交反思节点决策。
        """
        raise NotImplementedError


__all__ = [
    "ConfirmationRequired",
    "Tool",
    "ToolContext",
    "ToolError",
    "ToolParameter",
    "coerce_value",
    "openai_tool_schema",
    "parameters_metadata",
    "validate_parameters",
]
