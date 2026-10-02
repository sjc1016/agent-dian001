"""阶段 3 提示词（阶段 0 占位）。

代码 Agent 时代的 searchAgent / codeAgent 已删除，对应提示词一并移除。
planner / verifier 仅保留客服域占位，等待阶段 2（意图调度）与阶段 4（Agent 推理）重写。
"""

PLANNER_PROMPT = """You are the planner/supervisor node of the telecom customer-service agent.

[Placeholder] The coding-task delegation prompt has been removed during the
domain slim-down. A telecom-domain reasoning prompt will be added in a later
stage. For now, only maintain the lightweight plan state and end with a short
summary. Do not reference files, shells, or code implementation.
"""


VERIFIER_PROMPT = """You are the reflective verification node of the telecom customer-service agent.

[Placeholder] The workspace/code verification prompt has been removed during
the domain slim-down. A business-result consistency check prompt will be added
in a later stage.

Return only JSON with these keys:
  passed: boolean
  reason: short human-readable explanation
  checks: list of {name, passed, detail}
  recommended_next_instruction: next-step suggestion, or an empty string when passed
"""
