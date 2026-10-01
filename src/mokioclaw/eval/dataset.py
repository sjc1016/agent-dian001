"""P6-7：内置 30 条标注评测集。

覆盖四类业务（余额查询、套餐咨询/办理、故障报修、资费知识）+ 模糊意图 + 无关请求。

每条样本字段：
- id: 样本编号
- input: 用户输入
- expected_category: 期望意图类别（rag_query / agent_service / clarify / irrelevant）
- expected_route: 期望路由节点（rag_answer / agent_loop / clarify / fallback）
- expected_tools: 期望调用的 Skill 名称列表（可为空）
- expected_fallback: 是否期望触发兜底（irrelevant / clarify_exceeded / unknown_streak / none）
- max_clarify: 期望最大追问轮数（仅 clarify 类样本）
- notes: 标注备注
"""

from __future__ import annotations

from typing import Any


LABELED_DATASET: list[dict[str, Any]] = [
    # ===== 余额/话费查询（agent_service）8 条 =====
    {
        "id": "E01",
        "input": "查一下我话费余额",
        "expected_category": "agent_service",
        "expected_route": "agent_loop",
        "expected_tools": ["query_balance"],
        "expected_fallback": "none",
        "notes": "直接查余额，单工具",
    },
    {
        "id": "E02",
        "input": "我这个月话费花了多少",
        "expected_category": "agent_service",
        "expected_route": "agent_loop",
        "expected_tools": ["query_balance"],
        "expected_fallback": "none",
        "notes": "实时话费查询",
    },
    {
        "id": "E03",
        "input": "帮我看看账户里还剩多少钱",
        "expected_category": "agent_service",
        "expected_route": "agent_loop",
        "expected_tools": ["query_balance"],
        "expected_fallback": "none",
        "notes": "口语化余额查询",
    },
    {
        "id": "E04",
        "input": "查余额 13800138000",
        "expected_category": "agent_service",
        "expected_route": "agent_loop",
        "expected_tools": ["query_balance"],
        "expected_fallback": "none",
        "notes": "带号码查询",
    },
    {
        "id": "E05",
        "input": "我的话费还够不够用到月底",
        "expected_category": "agent_service",
        "expected_route": "agent_loop",
        "expected_tools": ["query_balance"],
        "expected_fallback": "none",
        "notes": "间接余额查询",
    },
    {
        "id": "E06",
        "input": "查一下当前套餐和余量",
        "expected_category": "agent_service",
        "expected_route": "agent_loop",
        "expected_tools": ["query_package"],
        "expected_fallback": "none",
        "notes": "套餐余量查询",
    },
    {
        "id": "E07",
        "input": "我套餐里流量还剩多少",
        "expected_category": "agent_service",
        "expected_route": "agent_loop",
        "expected_tools": ["query_package"],
        "expected_fallback": "none",
        "notes": "流量余量",
    },
    {
        "id": "E08",
        "input": "看看我现在用的什么套餐",
        "expected_category": "agent_service",
        "expected_route": "agent_loop",
        "expected_tools": ["query_package"],
        "expected_fallback": "none",
        "notes": "当前套餐查询",
    },
    # ===== 故障报修（agent_service）6 条 =====
    {
        "id": "E09",
        "input": "我家宽带断了帮我报修",
        "expected_category": "agent_service",
        "expected_route": "agent_loop",
        "expected_tools": ["report_fault"],
        "expected_fallback": "none",
        "notes": "宽带报修，建单",
    },
    {
        "id": "E10",
        "input": "宽带光猫亮红灯上不了网",
        "expected_category": "agent_service",
        "expected_route": "agent_loop",
        "expected_tools": ["report_fault"],
        "expected_fallback": "none",
        "notes": "故障现象描述+报修",
    },
    {
        "id": "E11",
        "input": "手机突然没信号了",
        "expected_category": "agent_service",
        "expected_route": "agent_loop",
        "expected_tools": ["report_fault"],
        "expected_fallback": "none",
        "notes": "手机信号故障",
    },
    {
        "id": "E12",
        "input": "帮我查一下报修工单进度",
        "expected_category": "agent_service",
        "expected_route": "agent_loop",
        "expected_tools": ["query_fault_status"],
        "expected_fallback": "none",
        "notes": "工单进度查询",
    },
    {
        "id": "E13",
        "input": "我上周报的宽带修好了吗",
        "expected_category": "agent_service",
        "expected_route": "agent_loop",
        "expected_tools": ["query_fault_status"],
        "expected_fallback": "none",
        "notes": "历史工单查询",
    },
    {
        "id": "E14",
        "input": "IPTV 看不了了，报修",
        "expected_category": "agent_service",
        "expected_route": "agent_loop",
        "expected_tools": ["report_fault"],
        "expected_fallback": "none",
        "notes": "IPTV 故障报修",
    },
    # ===== 套餐办理（agent_service，含写操作）4 条 =====
    {
        "id": "E15",
        "input": "帮我把套餐换成199元档",
        "expected_category": "agent_service",
        "expected_route": "agent_loop",
        "expected_tools": ["change_package"],
        "expected_fallback": "none",
        "notes": "套餐变更，需人工确认",
    },
    {
        "id": "E16",
        "input": "我要办理5G畅享129元档",
        "expected_category": "agent_service",
        "expected_route": "agent_loop",
        "expected_tools": ["change_package"],
        "expected_fallback": "none",
        "notes": "明确套餐名办理",
    },
    {
        "id": "E17",
        "input": "把我的套餐升级到5G畅享199元档",
        "expected_category": "agent_service",
        "expected_route": "agent_loop",
        "expected_tools": ["change_package"],
        "expected_fallback": "none",
        "notes": "升级套餐",
    },
    {
        "id": "E18",
        "input": "有哪些套餐可以办",
        "expected_category": "agent_service",
        "expected_route": "agent_loop",
        "expected_tools": ["list_packages"],
        "expected_fallback": "none",
        "notes": "可办理套餐列表",
    },
    # ===== 知识咨询（rag_query）6 条 =====
    {
        "id": "E19",
        "input": "5G畅享套餐包含多少流量",
        "expected_category": "rag_query",
        "expected_route": "rag_answer",
        "expected_tools": [],
        "expected_fallback": "none",
        "notes": "套餐内容知识问答",
    },
    {
        "id": "E20",
        "input": "国际漫游怎么收费",
        "expected_category": "rag_query",
        "expected_route": "rag_answer",
        "expected_tools": [],
        "expected_fallback": "none",
        "notes": "资费规则咨询",
    },
    {
        "id": "E21",
        "input": "宽带报修的流程是什么",
        "expected_category": "rag_query",
        "expected_route": "rag_answer",
        "expected_tools": [],
        "expected_fallback": "none",
        "notes": "办理流程咨询",
    },
    {
        "id": "E22",
        "input": "199档套餐有什么权益",
        "expected_category": "rag_query",
        "expected_route": "rag_answer",
        "expected_tools": [],
        "expected_fallback": "none",
        "notes": "套餐权益咨询",
    },
    {
        "id": "E23",
        "input": "流量加油包多少钱",
        "expected_category": "rag_query",
        "expected_route": "rag_answer",
        "expected_tools": [],
        "expected_fallback": "none",
        "notes": "加油包资费",
    },
    {
        "id": "E24",
        "input": "携号转网怎么办理",
        "expected_category": "rag_query",
        "expected_route": "rag_answer",
        "expected_tools": [],
        "expected_fallback": "none",
        "notes": "携号转网流程",
    },
    # ===== 模糊意图（clarify）3 条 =====
    {
        "id": "E25",
        "input": "我想换个更划算的",
        "expected_category": "clarify",
        "expected_route": "clarify",
        "expected_tools": [],
        "expected_fallback": "none",
        "max_clarify": 5,
        "notes": "意图模糊，需追问",
    },
    {
        "id": "E26",
        "input": "麻烦帮我处理一下那个",
        "expected_category": "clarify",
        "expected_route": "clarify",
        "expected_tools": [],
        "expected_fallback": "none",
        "max_clarify": 5,
        "notes": "指代不明",
    },
    {
        "id": "E27",
        "input": "你好",
        "expected_category": "clarify",
        "expected_route": "clarify",
        "expected_tools": [],
        "expected_fallback": "none",
        "max_clarify": 5,
        "notes": "寒暄无业务，引导说明需求",
    },
    # ===== 无关请求（irrelevant → fallback）3 条 =====
    {
        "id": "E28",
        "input": "帮我写一首诗",
        "expected_category": "irrelevant",
        "expected_route": "fallback",
        "expected_tools": [],
        "expected_fallback": "irrelevant_request",
        "notes": "无关请求直接兜底",
    },
    {
        "id": "E29",
        "input": "今天天气怎么样",
        "expected_category": "irrelevant",
        "expected_route": "fallback",
        "expected_tools": [],
        "expected_fallback": "irrelevant_request",
        "notes": "非电信业务",
    },
    {
        "id": "E30",
        "input": "帮我写个 Python 爬虫",
        "expected_category": "irrelevant",
        "expected_route": "fallback",
        "expected_tools": [],
        "expected_fallback": "irrelevant_request",
        "notes": "写代码请求，无关",
    },
]


def get_dataset() -> list[dict[str, Any]]:
    """返回标注评测集副本。"""
    return [dict(item) for item in LABELED_DATASET]
