"""阶段 6：自动化评测流水线。

模块组成：
- :mod:`collector` —— 从 trace 的 events.jsonl 采集会话轨迹
- :mod:`normalizer` —— 归一化为标准评测样本
- :mod:`rule_checks` —— 规则校验（分流/参数/兜底/追问轮次）
- :mod:`llm_judge` —— LLM-Judge 打分（质量/推理/合规）
- :mod:`report` —— Markdown 评测报告生成
- :mod:`dataset` —— 内置 30 条标注评测集
"""

from __future__ import annotations
