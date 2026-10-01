"""阶段 4：Agent 深度推理引擎（LangGraph 子图）。

拓扑见 :mod:`mokioclaw.agent.workflow`：
approval_entry（跨轮确认恢复）→ think（思考）→ act（多工具并行编排）
→ reflect（业务一致性反思）→ finalize（自然语言答复），
异常/校验失败按 attempts 阈值走 agent_fallback 兜底。
"""

from __future__ import annotations
