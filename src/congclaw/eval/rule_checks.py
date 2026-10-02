"""P6-4：规则校验——硬规则断言。

四类断言：
1. 分流正确性：实际意图类别/路由与期望一致
2. 工具参数合法性：被调用 Skill 的必填参数齐全、枚举值合法
3. 兜底触发：无关/超限应兜底，业务类不应兜底
4. 追问轮次：clarify 类样本追问 ≤ 5 轮
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from congclaw.eval.normalizer import EvalSample

CLARIFY_MAX_ROUNDS = 5


@dataclass
class RuleResult:
    """单条规则校验结果。"""

    name: str
    passed: bool
    detail: str = ""


@dataclass
class RuleCheckReport:
    """一条样本的全部规则校验结果。"""

    sample_id: str
    passed: bool
    results: list[RuleResult] = field(default_factory=list)

    @property
    def failures(self) -> list[RuleResult]:
        return [r for r in self.results if not r.passed]


def run_rule_checks(sample: EvalSample) -> RuleCheckReport:
    """对单条 EvalSample 执行全部规则校验。"""
    results: list[RuleResult] = []
    results.append(_check_routing(sample))
    results.append(_check_tool_params(sample))
    results.append(_check_fallback(sample))
    results.append(_check_clarify_rounds(sample))
    return RuleCheckReport(
        sample_id=sample.sample_id,
        passed=all(r.passed for r in results),
        results=results,
    )


def _check_routing(sample: EvalSample) -> RuleResult:
    """分流正确性：类别与路由均需匹配期望。"""
    cat_ok = sample.actual_category == sample.expected_category
    route_ok = sample.actual_route == sample.expected_route
    passed = cat_ok and route_ok
    detail = ""
    if not cat_ok:
        detail += f"category: expected={sample.expected_category}, actual={sample.actual_category}; "
    if not route_ok:
        detail += f"route: expected={sample.expected_route}, actual={sample.actual_route}"
    return RuleResult(name="routing", passed=passed, detail=detail.strip())


def _check_tool_params(sample: EvalSample) -> RuleResult:
    """工具参数合法性：必填参数齐全、枚举值合法。"""
    if not sample.tool_calls:
        # 无工具调用时，若期望也无工具则通过；否则标记缺失
        if sample.expected_tools:
            return RuleResult(
                name="tool_params",
                passed=False,
                detail=f"expected tools {sample.expected_tools} but none called",
            )
        return RuleResult(name="tool_params", passed=True, detail="no tools expected or called")

    errors: list[str] = []
    for call in sample.tool_calls:
        name = str(call.get("name") or "")
        args = call.get("args") or {}
        if not isinstance(args, dict):
            args = {}
        errors.extend(_validate_skill_args(name, args))

    # 同时检查期望工具是否被调用
    missing = [t for t in sample.expected_tools if t not in sample.actual_tools]
    if missing:
        errors.append(f"missing expected tools: {missing}")

    return RuleResult(
        name="tool_params",
        passed=not errors,
        detail="; ".join(errors) if errors else "all tool calls valid",
    )


def _validate_skill_args(skill_name: str, args: dict[str, Any]) -> list[str]:
    """根据 Skill 注册表的参数声明校验入参。"""
    errors: list[str] = []
    try:
        from congclaw.skills.registry import get_registry

        registry = get_registry()
        skill = registry.try_get(skill_name)
    except Exception:
        skill = None

    if skill is None:
        # 未注册的 Skill（可能已热卸载）不做参数硬校验
        return errors

    declared = {p.name: p for p in skill.parameters}
    for pname, param in declared.items():
        value = args.get(pname, param.default)
        if value is None or (isinstance(value, str) and not value.strip()):
            if param.required:
                errors.append(f"{skill_name}: missing required param '{pname}'")
            continue
        if param.enum is not None and value not in param.enum:
            errors.append(
                f"{skill_name}: param '{pname}'={value!r} not in enum {param.enum}"
            )
    return errors


def _check_fallback(sample: EvalSample) -> RuleResult:
    """兜底触发断言。"""
    expected_fb = sample.expected_fallback
    actual_fb = sample.actual_fallback

    if expected_fb == "none":
        # 业务类样本不应兜底（RAG 无证据兜底除外，但标注集中业务类均期望有答案）
        passed = actual_fb == "none"
        detail = "" if passed else f"unexpected fallback: {actual_fb}"
    else:
        # 无关/超限样本应兜底
        passed = actual_fb != "none"
        detail = "" if passed else f"expected fallback '{expected_fb}' but got none"

    return RuleResult(name="fallback", passed=passed, detail=detail)


def _check_clarify_rounds(sample: EvalSample) -> RuleResult:
    """追问轮次 ≤ 5。"""
    rounds = sample.clarify_rounds
    passed = rounds <= CLARIFY_MAX_ROUNDS
    return RuleResult(
        name="clarify_rounds",
        passed=passed,
        detail=f"clarify rounds={rounds}, max={CLARIFY_MAX_ROUNDS}",
    )
