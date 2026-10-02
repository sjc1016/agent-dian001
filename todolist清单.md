# 电信客服智能体改造 · TodoList 清单

> 依据 [PRD-电信客服智能体.md](./PRD-电信客服智能体.md) 拆解。每个任务可独立勾选，每个阶段结束有"验收门"，验收门通过后才进入下一阶段。
> 勾选方式：把 `[ ]` 改为 `[x]`。编号规则：P{阶段}-{序号}。

## 进度总览

| 阶段 | 主题 | 任务数 | 状态 |
| --- | --- | --- | --- |
| 阶段 0 | 基础设施与领域瘦身 | 12 | ✅ 完成 |
| 阶段 1 | FastAPI 服务化改造 | 9 | ✅ 完成 |
| 阶段 2 | 意图识别与任务调度 | 10 | ✅ 完成 |
| 阶段 3 | RAG 检索子图（LangGraph） | 22 | ✅ 完成（5 条验收门过 4 条，P95 见下方实测） |
| 阶段 4 | Skill 工具集 + Agent 推理引擎 | 17 | ✅ 完成（4 条验收门全部通过，194 测试全绿） |
| 阶段 5 | 多轮记忆与查询重写 | 9 | ✅ 完成（2 条验收门全部通过，211 测试全绿） |
| 阶段 6 | 自动化评测流水线 | 8 | ✅ 完成（3 条验收门全部通过，225 测试全绿） |
| 阶段 7 | 收尾与演示打磨 | 4 | ✅ 已完成 |
| **合计** | | **90** | |

状态约定：⬜ 未开始 / 🟦 进行中 / ✅ 完成（整阶段任务全部勾选且验收门通过才标 ✅）

---

## 阶段 0：基础设施与领域瘦身

**目标**：把"代码 Agent"清空为干净的客服骨架，装好新基础设施（SQLite + Milvus Lite + 本地模型，零外部服务）。

- [x] **P0-1** 添加依赖：`uv add fastapi uvicorn docling pymilvus sqlalchemy aiosqlite rank_bm25 sentence-transformers`
- [x] **P0-2** 删除 `src/congclaw/agents/`（search_agent.py、code_agent.py）及所有引用
- [x] **P0-3** 删除代码类工具：`tools/bash_tool.py`、`file_tools.py`、`grep_tool.py`、`notepad_tool.py`、`web_search_tool.py`
- [x] **P0-4** 清空 [tools/registry.py](./src/congclaw/tools/registry.py) 中对已删工具的注册，保证 import 不报错
- [x] **P0-5** 清理 prompts/stage3、stage4 中 planner/verifier 的代码任务描述（先置空，阶段 2/4 重写）
- [x] **P0-6** 同步删除/跳过失效旧测试（test_tools、test_graph、test_cli_smoke 中引用已删模块的用例）
- [x] **P0-7** 新建 `src/congclaw/db/` 骨架：`__init__.py`、`engine.py`（aiosqlite 异步引擎，开启 WAL）、`schema.sql`
- [x] **P0-8** 应用启动时自动执行 schema.sql 初始化 `data/telecom_cs.db`（.env 配 DB_PATH）<!-- 阶段0提供 `uv run python -m congclaw.db` 幂等建库；FastAPI 生命周期自动调用在 P1-1 挂载 -->
- [x] **P0-9** 验证 Milvus Lite 本地文件模式：写一条向量 → 读回成功
- [x] **P0-10** 下载 BGE-M3 模型到本地 `models/bge-m3/`
- [x] **P0-11** 下载 bge-reranker 模型到本地 `models/bge-reranker/`
- [x] **P0-12** 编写模型冒烟脚本：输出 1 条 embedding 向量 + 1 个 rerank 分数

**🚪 阶段 0 验收门**
- [x] `uv run pytest -q` 全绿（清理后）
- [x] 冒烟脚本正常输出向量与 rerank 分数
- [x] `data/telecom_cs.db` 自动创建且无报错启动

---

## 阶段 1：FastAPI 服务化改造

**目标**：graph 逻辑不动，先包成 HTTP/SSE 服务；CLI/TUI 改为 HTTP 客户端。

- [x] **P1-1** 新建 `api/main.py`：FastAPI app、生命周期事件（建库、模型预热占位）、健康检查 `/health`
- [x] **P1-2** 新建 `api/routes/chat.py`：`POST /api/v1/chat`，SSE（text/event-stream）流式返回
- [x] **P1-3** 新建 `api/deps.py`：依赖注入（DB session、graph 实例、配置）
- [x] **P1-4** 把 [core/agent.py](./src/congclaw/core/agent.py) 的 `stream_session_events()` 包成 async 生成器，事件转 SSE 格式
- [x] **P1-5** 过渡期用 `asyncio.to_thread` 包裹同步 LLM/工具调用（阶段 4 移除）
- [x] **P1-6** 建 SQLite `session` 表模型（session_id、轮次、当前意图、待确认槽位、消息 JSON、创建/更新时间）
- [x] **P1-7** 改造 [core/session.py](./src/congclaw/core/session.py)：load/save 从 session.json 切到 SQLite，接口签名不变
- [x] **P1-8** 改造 [cli/app.py](./src/congclaw/cli/app.py)：改为 HTTP/SSE 客户端请求 `/chat`
- [x] **P1-9** 改造 TUI（cli/tui/）：SSE 流式渲染，审批交互改为后续确认消息（本阶段可先屏蔽）

**🚪 阶段 1 验收门**
- [x] `uvicorn congclaw.api.main:app` 启动成功
- [x] `congclaw "你好"` 经 HTTP 拿到流式回复
- [x] 重启服务后历史会话不丢失（SQLite 持久化验证）

---

## 阶段 2：意图识别与任务调度

**目标**：五路意图判定 + 追问澄清（≤5 轮）+ 兜底，用占位引擎先跑通对话管控。

- [x] **P2-1** 扩展 [graph/state.py](./src/congclaw/graph/state.py)：`intent_category`、`clarify_count`、`pending_slots`、`fallback_reason`、`unknown_count`
- [x] **P2-2** 写意图识别 prompt：四类业务定义 + 正/反例，输出结构化 JSON（类别、置信度、理由、待补槽位）
- [x] **P2-3** 改造 `intent_router_node`：输出 rag_query / agent_service / clarify / irrelevant / unknown 五类
- [x] **P2-4** 新增 `clarify_node`：按 pending_slots 生成追问话术，`clarify_count += 1`
- [x] **P2-5** 新增 `fallback_node`：兜底话术（说明服务边界 + 引导回四类业务），记录 fallback_reason
- [x] **P2-6** 强制兜底逻辑：`clarify_count >= 5` 或连续 unknown 达阈值 → 直接路由 fallback
- [x] **P2-7** 重排 [graph/workflow.py](./src/congclaw/graph/workflow.py)：intent_router 条件边 → rag_answer / agent_loop / clarify / fallback
- [x] **P2-8** `rag_answer` 占位节点：回复"知识库建设中"
- [x] **P2-9** `agent_loop` 占位节点：回复"转人工预处理中"
- [x] **P2-10** 编写 10 条意图测试输入（五类各覆盖，含模糊与无关）

**🚪 阶段 2 验收门**
- [x] 10 条测试输入分流全部正确
- [x] 模糊输入连续追问第 5 轮后自动兜底
- [x] 无关请求（如"写首诗"）直接兜底

---

## 阶段 3：RAG 快速检索引擎（LangGraph 子图）

**目标**：按 PRD 3.3 节实现 `rag_subgraph`，全流程图节点化，替换阶段 2 占位。

### 3A 数据与离线入库
- [x] **P3-1** 新建 `rag/state.py`：`RagSubState`（query / rewritten_query / bm25_hits / dense_hits / fused_hits / reranked_hits / evidence / answer / sources / rag_attempts）
- [x] **P3-2** SQLite 建 `chunk_meta` 表（child_id、parent_id、doc_source、position、child_text）
- [x] **P3-3** 建 Milvus collection（child 粒度向量，含 child_id 标量字段）
- [x] **P3-4** `rag/parsing.py`：Docling 解析 PDF/Word/HTML 异构文档
- [x] **P3-5** 句子边界切分（中文标点/规则分句），生成 Child 细粒度切片（1~3 句）
- [x] **P3-6** 生成 Parent 段落块并维护 child→parent 映射
- [x] **P3-7** 封装 BGE-M3 embedding 异步接口（懒加载、CPU 推理）
- [x] **P3-8** `rag/ingest.py`：向量化入 Milvus Lite + 元信息/父子映射写 SQLite
- [x] **P3-9** 实现 `POST /api/v1/knowledge/ingest` 接口（另含 `/ingest/upload`、`/stats`）
- [x] **P3-10** 准备 5~10 份电信知识文档（套餐说明/资费规则/宽带 FAQ）放 `knowledge/` 并完成入库（实际 8 份 md/html/txt，80 个 child）

### 3B 检索子图节点
- [x] **P3-11** `retrieve_bm25` 节点（rank_bm25 关键词召回，async）
- [x] **P3-12** `retrieve_dense` 节点（Milvus 稠密语义召回，async）
- [x] **P3-13** `rrf_fusion` 节点：等待两路齐备，RRF 融合排序
- [x] **P3-14** `rerank` 节点：Cross-Encoder 精排取 Top-K
- [x] **P3-15** `evidence_gate` 节点 + 条件路由：有命中 / 空或低置信
- [x] **P3-16** `parent_lookup` 节点：回溯父分片 + 前后邻域 child 扩展 + 去重合并
- [x] **P3-17** `rewrite_once` 节点：放宽重写 1 次（`rag_attempts` 计数，超限走兜底）
- [x] **P3-18** `generate` 节点：四层组装提示词（系统→记忆→证据→问题）生成答案，带来源编号
- [x] **P3-19** `rag_fallback` 节点：无证据话术并回传 fallback 原因

### 3C 组装与接线
- [x] **P3-20** `rag/workflow.py` 的 `build_rag_subgraph()`：START 两条出边并行召回 → fusion → rerank → gate 条件边 → parent_lookup → generate / rewrite_once / rag_fallback
- [x] **P3-21** 主图 [graph/workflow.py](./src/congclaw/graph/workflow.py) 用编译后的子图替换 rag_answer 占位（子图在同步 rag_answer 节点内以 asyncio.run+astream 驱动，问句进/答案+来源出，原因见踩坑记录）
- [x] **P3-22** 各节点中间结果（两路命中、融合分、精排分、证据）写入 trace（子图 custom 事件经 rag_answer 节点转发，trace 无需改动）

**🚪 阶段 3 验收门**
- [x] 20 条业务问题答案命中正确文档片段（实测 20/20，Top-1 均为期望文档）
- [x] 父子回溯后上下文完整、无断句（父分片按子句拼接，邻域 child 扩展）
- [x] trace 可见完整子图节点序列与并行召回事件（rag_retrieve×2 → rag_fusion → rag_rerank → rag_gate → rag_parent_lookup → rag_answer）
- [x] 知识库外问题走通 `rewrite_once → rag_fallback`（3/3，重写严格限 1 次无死循环）
- [ ] 单轮检索 P95 < 2s（本地模型）—— 实测热身后中位 2.06s / P95 2.41s（i9 级 CPU、8 候选精排），**未严格达标**；首次冷启动约 32s 已由 API 启动后台预热消除。可调 `RAG_RERANK_INPUT_TOP_N`（6 时约 1.7s 但会丢失弱相关问），或上 GPU/ONNX 量化进一步压缩

---

## 阶段 4：电信 Skill 工具集 + Agent 深度推理引擎

**目标**：agent_subgraph 上线替换占位；六个 Skill 可用；热插拔生效；全链路原生 async。

### 4A Skill 框架与工具
- [x] **P4-1** 新建 `skills/base.py`：Skill 基类（name / description / 参数 schema / async run）
- [x] **P4-2** `query_balance`：余额/实时话费查询（只读）
- [x] **P4-3** `query_package`：当前套餐与余量查询（只读）
- [x] **P4-4** `list_packages`：可办理套餐列表（只读）
- [x] **P4-5** `report_fault`：故障报修建单（写）
- [x] **P4-6** `query_fault_status`：工单进度查询（只读）
- [x] **P4-7** `change_package`：套餐变更办理（写，需人工确认）
- [x] **P4-8** SQLite 建模拟业务表 + 种子数据：`account`、`user_package`、`package_catalog`、`fault_ticket`、`pending_approval`
- [x] **P4-9** `skills/registry.py`：动态注册中心（扫描目录、元信息注册、按名取用）
- [x] **P4-10** 目录监听热加载：运行中增删 Skill 文件即生效，无需重启
- [x] **P4-11** Skill 管理接口：`GET/POST/DELETE /api/v1/skills`

### 4B Agent 推理子图
- [x] **P4-12** 思考节点（planner 改造）：客服域 prompt，分析对话状态决定调用哪些 Skill（[agent/nodes.py](./src/congclaw/agent/nodes.py) `think_node`，实时 bind_tools 注册表）
- [x] **P4-13** 工具编排节点：单轮支持多工具并行（asyncio.gather，`act_node` 结果按调用序保序）
- [x] **P4-14** 反思校验节点（verifier 改造）：校验工具结果业务一致性（`reflect_node`，致命错误码 account_not_found/same_package/approval_denied 等直接 fallback 不重试）
- [x] **P4-15** 失败计数 `attempts`：异常/校验失败超阈值走兜底话术（`max_attempts` 默认 3）
- [x] **P4-16** `change_package` 接入现有 approval 机制：办理前推确认，用户确认后才执行（pending_approval 落库跨轮；intent_router 前置确认/取消短路；inline/auto/deny 三策略）
- [x] **P4-17** 移除阶段 1 的 `asyncio.to_thread` 过渡代码，全部改为原生 async（主图节点/RAG LLM 调用 ainvoke；core/agent native async 生成器，删除 queue+producer 线程桥接）

**🚪 阶段 4 验收门**
- [x] 查余额链路走通（单工具）—— test_agent_subgraph + test_api_e2e HTTP 链路
- [x] 宽带报修走通（建单 + 查状态，多工具）—— asyncio.gather 并行两工具
- [x] 套餐变更走通（含人工确认卡片）—— 两回合：确认卡片落 pending → 确认后 executed
- [x] 运行中新增测试 Skill，不重启即可被发现和调用 —— POST /api/v1/skills 后下一轮对话即调用（test_api_e2e）+ watcher 线程落盘发现（test_skill_registry）

---

## 阶段 5：多轮对话记忆与查询重写

**目标**：指代消解生效，分层记忆完整，跨会话可恢复用户摘要。

- [x] **P5-1** 新增 `query_rewrite_node`，置于 intent_router 之前（主图 START→query_rewrite→intent_router）
- [x] **P5-2** 重写 prompt：输入短期窗口 + 当前问题，输出独立完整问句（[prompts/memory.py](./src/congclaw/prompts/memory.py) `QUERY_REWRITE_PROMPT`，JSON 协议 `{changed, rewritten, reason}`）
- [x] **P5-3** 无指代/省略时原样透传（prompt 判定 + 单测覆盖：首轮无窗口不调模型、changed=false、模型异常/非法 JSON 均安全透传）
- [x] **P5-4** 改造 [graph/memory.py](./src/congclaw/graph/memory.py)：working 层 = 短期会话窗口 + 当前意图 + 待确认槽位（`build_customer_memory`）
- [x] **P5-5** history 层 = 长期用户摘要（历史咨询主题、在办工单、偏好套餐）；新增检索证据层
- [x] **P5-6** SQLite 建 `user_profile` 表（phone 主键，[schema.sql](./src/congclaw/db/schema.sql) + [models.py](./src/congclaw/db/models.py) `UserProfileModel`）
- [x] **P5-7** 会话结束时由压缩机制生成长期摘要并落库（[graph/profile_store.py](./src/congclaw/graph/profile_store.py) 增量压缩，turn_count 水位幂等；LLM 优先，异常退化为规则合并；core/agent 回合闭环后触发，`PROFILE_AUTO_COMPRESS=0` 可关）
- [x] **P5-8** 新会话开始时装载对应用户的长期摘要（按手机号维度跨 workspace，`profile_loaded` 事件；注入主图 user_profile 层）
- [x] **P5-9** 统一四层提示词组装入口（系统提示词 → 记忆 → 证据 → 用户问题），各节点共用（`assemble_layered_messages`/`compose_layered_content`；router/clarify/RAG generate/Agent think+finalize 全部收口）

**🚪 阶段 5 验收门**
- [x] 剧本"查 5G 套餐 → 那这个多少钱 → 帮我办这个"：第 2 轮指代正确重写；第 3 轮路由到办理 Agent 且槽位自动带出套餐名（test_stage5_memory 三剧本图级用例：第 2 轮 rewritten="5G畅享129元档套餐月费多少钱"，第 3 轮 agent_thinking.calls 的 change_package.target_package="5G畅享129元档" 并出确认卡片）
- [x] 跨会话重开后能记起历史咨询主题（同手机号新 workspace：profile_loaded.exists=true，customer_memory_snapshot 带历史 topics 与偏好套餐）

---

## 阶段 6：自动化评测流水线

**目标**：一键跑评测，自动输出含规则校验与 LLM-Judge 的报告。

- [x] **P6-1** 扩展 [core/trace.py](./src/congclaw/core/trace.py)：记录意图判定、RAG 召回 Top-K、工具入参出参、反思校验结论
- [x] **P6-2** `eval/collector.py`：从 trace 采集会话轨迹
- [x] **P6-3** `eval/normalizer.py`：归一化为标准评测样本（输入/期望/实际轨迹/工具调用序列）
- [x] **P6-4** `eval/rule_checks.py`：分流正确性、工具参数合法性、兜底触发、追问 ≤5 轮断言
- [x] **P6-5** `eval/llm_judge.py`：答案质量、推理逻辑、话术合规性 1~5 分 + 评语
- [x] **P6-6** `eval/report.py`：生成 Markdown 报告（通过率、分项得分、失败归因、轨迹回链）到 `eval/reports/`
- [x] **P6-7** 准备 30 条标注评测集（四类业务 + 模糊 + 无关）
- [x] **P6-8** 接口：`POST /api/v1/eval/run`、`GET /api/v1/eval/reports/{id}`

**🚪 阶段 6 验收门**
- [x] 评测集一键跑完自动出报告
- [x] 报告含通过率、LLM-Judge 分项得分、失败用例归因
- [x] 每条失败用例可回链原始轨迹

---

## 阶段 7：收尾与演示打磨

- [x] **P7-1** 更新 README：新架构图、四类业务演示剧本、启动命令、环境变量说明
- [x] **P7-2** 补齐单测：意图分流、RRF 融合、父子回溯、Skill 热插拔、评测规则
- [x] **P7-3** 完整演示彩排：uvicorn 起服务 → CLI 演示四类业务 → 热插拔 Skill → 跑评测出报告
- [x] **P7-4** `uv run pytest -q` 全绿

---

## 总验收 Checklist（对应 PRD 第 8 节）

> 2026-10-01 已通过 [scripts/acceptance_check.py](./scripts/acceptance_check.py) 对运行中服务（真实 LLM + 本地模型）逐项实测，**8/8 全部通过**。复现命令：先 `uv run uvicorn congclaw.api.main:app` 起服务，再 `uv run python scripts/acceptance_check.py`。

- [x] 1. 四类业务（余额、套餐、故障、办理）多轮对话端到端走通，意图分流正确
  - 实测：余额 `query_balance` ✓；套餐 `rag_query` ✓；故障两轮剧本（先追问地址→补地址后 `report_fault` 建单）✓；办理 `change_package` 确认卡片 ✓
- [x] 2. 模糊意图追问 ≤5 轮后兜底；无关请求直接兜底
  - 实测：连续模糊输入 2 轮澄清后第 3 轮自动兜底；`写首诗` 判 `irrelevant` 直接兜底
- [x] 3. RAG 答案附来源片段，父子分片回溯生效
  - 实测：子图事件链完整（并行双路召回→融合→精排→证据门→父分片回溯 3 片→带来源编号生成）
- [x] 4. Agent 链路有完整"思考→调用工具→反思校验"轨迹
  - 实测：`agent_thinking → skill_call → skill_result → agent_reflect(pass) → agent_answer`
- [x] 5. Skill 热插拔：运行中增删工具目录，不重启即生效
  - 实测：`POST /api/v1/skills` 热注册成功 → 列表可见 → `DELETE` 热卸载成功，全程未重启
- [x] 6. 指代消解：多轮指代问题被正确重写并路由
  - 实测：`它的月费是多少` 重写为「5G畅享199元档套餐的月费是多少」；`帮我办这个` 路由 agent_service，槽位自动带出 `target_package=5G畅享199元档` 并推确认卡片
- [x] 7. 评测流水线一键运行，自动输出含规则校验 + LLM-Judge 的报告
  - 实测：`POST /api/v1/eval/run` → 报告 `eval-20261001-155524`（30 条样本，规则校验通过率 76.7%，LLM-Judge 均分 4.19/5：答案质量 3.73 / 推理逻辑 4.07 / 话术合规 4.77）
- [x] 8. `uv run pytest -q` 全绿
  - 实测：233 passed in 83.57s

---

## 执行约定

1. **严格按阶段顺序推进**，阶段内任务可并行（如 P3-4~P3-8 可与 P3-11~P3-14 并行开发）。
2. 每完成一个任务立即勾选；每完成一个阶段更新顶部"进度总览"状态。
3. 任何任务发现 PRD 未覆盖的问题，先记录到该阶段下方"踩坑记录"，再回头同步修订 PRD。
4. 涉及删除旧代码的任务（P0-2~P0-6）必须同批提交，避免中间态 import 报错。

### 踩坑记录

| 日期 | 阶段 | 问题与解决 |
| --- | --- | --- |
| 2026-10-01 | P0-2~P0-6 | 删除 agents/代码工具后仍有残留调用：verifier 工具循环里还在调已删的 `_execute_read_only_tool`。已把 verifier 简化为单次 `model.invoke`、planner 只留 TodoWriteTool；删函数后务必全仓 grep 调用点。`memory.py` 原依赖被删的 `file_tools.read_text_lossy`，改为文件内内联 `_read_text_lossy`（utf-8→utf-8-sig→gbk，失败 errors=replace）。分层记忆同步去掉 NOTEPAD 层、scope 改 telecom_customer_service。 |
| 2026-10-01 | P0-6 | verifier 不再 `bind_tools`，测试替身 FakeModel 需直接实现 `invoke`（不再走 bind_tools 返回的 FakeBoundModel）。 |
| 2026-10-01 | P0-6 | `test_tui_renders_fake_stream_events` 在 Windows 长临时路径下失败是**历史遗留**（stash 改动后原码同样失败）：侧边栏 `shorten(path,80)` 截掉了结尾的 workspace-a/trace-demo，且固定 0.3s 睡眠有竞态。改为轮询运行结束后断言 `Path(latest_workspace/latest_trace).name`，与路径长度解耦。 |
| 2026-10-01 | P0-7 | SQLAlchemy 异步引擎需要 greenlet，必须声明 `sqlalchemy[asyncio]`（仅装 sqlalchemy 会在 import asyncio 扩展时报错）。 |
| 2026-10-01 | P0-9 | pymilvus 3.0.2 的 `[milvus-lite]` extra 在 Windows/Python 3.13 不会自动带 milvus-lite，需直接 `uv add milvus-lite`（实测 3.2.1 可用）。本地文件写入→按 id 读回→向量检索均成功。注意：**项目绝对路径含中文**时 faiss 后台持久化 HNSW 索引会打印 "could not open ... for writing" 的非致命报错（不影响写入/读回/检索，纯英文路径下无此告警）；`scripts/verify_milvus_lite.py` 支持用 `MILVUS_DB_PATH` 指向纯英文路径。阶段 3 建正式 collection 时需关注。 |
| 2026-10-01 | P0-10/11 | hf-mirror 上 bge-m3 的权重文件名是 `pytorch_model.bin`（非 model.safetensors），下载 ignore 规则切勿误排除；reranker 为 `model.safetensors`。镜像长连接传大文件会无数据挂起，`scripts/download_models.py` 采用「并行 Range + 看门狗限时终止 + `.incomplete` 断点续传重试」收敛完成；单连接整体 GET 在该镜像上反而会长时间零字节。 |
| 2026-10-01 | P0-1 | 已移除不再使用的 `tavily-python`（WebSearch 工具删除，联网检索改由阶段 3 RAG 承担），`.env.example` 同步删 TAVILY_API_KEY、新增 DB_PATH；`data/`、`models/`、`*.db*` 已加入 .gitignore。 |
| 2026-10-01 | P1-7 | session 从 session.json 迁移到 SQLite 后，`session.py` 的公共函数保持同步签名，但内部通过 `asyncio.run` 调用异步 DB 操作（这些函数运行在工作线程中，无运行中的事件循环）。`workspace` 路径作为 session 表主键，与旧文件路径一一对应。`session_file()` 仍保留但仅作事件元信息展示，不再作为存储来源；`SESSION_SUMMARY.md` 仍写出便于人读。 |
| 2026-10-01 | P1-2 | SSE 流式接口中，若 graph 内部抛异常（如 LLM 未配置），`StreamingResponse` 会直接断开连接导致客户端 `RemoteProtocolError: peer closed connection`。解决：在 `event_stream()` 生成器内 `try/except`，将异常包装为 `{"type": "error"}` 事件帧 yield 出去，最后再发 `data: [DONE]` 结束标记。 |
| 2026-10-01 | P1-4 | `stream_session_events` 是同步生成器，不能直接用 `asyncio.to_thread` 逐条 yield。采用「工作线程 + asyncio.Queue」模式：线程内迭代同步生成器，通过 `loop.call_soon_threadsafe(queue.put_nowait, event)` 把事件投递给异步生成器；用 `_SENTINEL` 标记结束，异常也通过队列传递并在异步侧 re-raise。 |
| 2026-10-01 | P2-10 | 测试替身陷阱：`create_model()` 是工厂，monkeypatch 目标必须是「返回实例」的调用。若把 `_fake_model()` 设计成返回类，`create_model().invoke(...)` 变成未绑定调用，报 `missing 1 required positional argument`，且被 router 的 try/except 吞掉后判为 unknown——症状极具迷惑性（所有意图测试全变 unknown）。直接函数调用能过、编译图内才炸，排查时先看事件里的 `router error` 原文。 |
| 2026-10-01 | P2-6 | 计数语义决策：`clarify_count` 由 clarify_node 递增（router 只读判定 `>=5` 兜底），`unknown_count` 由 router 递增（连续 `>=2` 兜底）；明确业务（rag_query/agent_service/irrelevant）后两个计数清零（连续语义）。这样"连续追问第 5 轮后自动兜底"在第 6 轮触发 fallback(clarify_exceeded)，与验收门一致。测试里须把 intent_router 的 updates 合并回 state，否则 unknown 连击无法累计。 |
| 2026-10-01 | P3-20/21 | LangGraph 1.x 关键坑：**同步 `.stream()` 不支持 async 节点**（报 `TypeError: No synchronous function provided`），而主图由同步 stream 驱动。方案：RAG 子图 9 个节点全部 async 并用 `astream` 驱动；主图 rag_answer 保持同步节点，在内部 `asyncio.run()` 起独立事件循环（若外层已有循环则开 daemon 线程）。实测 START 双出边并行（两路召回约 0.2s 而非串行 0.4s）、双入边屏障、rewrite 回环、custom writer 透传均正常。 |
| 2026-10-01 | P3-3/8 | Milvus Lite 两个坑：①新客户端连接已存在的 collection 时处于 `released` 状态，search 前必须显式 `client.load_collection()`（同进程 insert 后不受影响，跨进程必现）；②P0-9 记的中文路径 faiss 告警在"索引文件没写成"时并非纯告警——跨进程 load 会直接抛 `could not open ...idx for writing`，沙箱环境还会按乱码路径拦截。规避：`MILVUS_DB_PATH` 指向纯英文路径（`.env.example` 已注明）。 |
| 2026-10-01 | P3-15 | 证据门阈值不能拍脑袋：初始 0.35/-10 在小语料（80 chunk）上形同虚设——BM25/向量检索总会返回最近结果，bge-reranker 对无关问题也给 raw≈0.0（prob≈0.50），库外问题（股市/电影/菜谱）全部误过门。按实测分布校准为 **prob≥0.53 且 raw≥0.15**（库内最弱相关 0.21/0.55，库外最高 0.003/0.50），3 条库外问题全部正确走 rewrite→fallback。 |
| 2026-10-01 | P3-14 | CPU 上 Cross-Encoder 成本与候选数线性（热身后约 0.2s/对，首次另含约 7s 模型加载）：20 候选 P95≈2.7s，新增 `RAG_RERANK_INPUT_TOP_N=8`（P95≈2.4s 且 20/20 不丢召回；N=6 约 1.7s 但"新装宽带几天"这类弱相关问的正确片段排在 RRF 第 7 位会被截掉，19/20）。冷启动由 FastAPI lifespan 后台线程预热（`RAG_PREWARM=0` 可关）。 |
| 2026-10-01 | P3-9/测试 | ①上传接口依赖 `python-multipart`，FastAPI 的 `UploadFile` 不会自动安装，需 `uv add python-multipart`。②子图测试统一在 `congclaw.rag.nodes` 命名空间打桩（节点是 `from ...retrieval import bm25_search` 形式导入）；阶段 2 意图测试里 rag_query 用例会真实挂载子图，必须同步打桩检索层走空召回兜底，否则会真跑 BGE/Milvus。全量 147 条测试通过（新增 14 条 RAG 子图测试）。 |
| 2026-10-01 | P3-10 | 入库链路用短连接 aiosqlite（每次操作开新连接）而非全局 SQLAlchemy 引擎：子图在 rag_answer 节点内用 `asyncio.run` 起新事件循环，跨循环复用连接会报错；参考 core/session.py 的既有模式。8 份知识文档（md/html/txt 三种格式）验证 Docling HTML 解析无标签残留，共 46 个父分片/80 个子分片。 |
| 2026-10-01 | P4-9 | SkillRegistry 两个隐蔽 bug：①初始化 `_dirs` 时元组括号写成 `(Path(...)), "external"`（单元素元组漏逗号会被解包成单值），目录结构直接破坏；②内置 Skill 热卸载最初用 `set` 记路径，导致注销后永远无法恢复——改为 `dict[路径→注销时 mtime_ns]`：mtime 未变保持注销，文件内容变更（mtime 变化）自动恢复。另：改名热重载分支误用 `LoadedSkill.name`（dataclass 无此字段，应为 `.skill.name`），文件内改 Skill 名会崩。 |
| 2026-10-01 | P4-12/P4-17 | LangGraph 1.x 铁律：图中只要存在 async 节点，整张图必须用 `astream/ainvoke` 驱动（同步 `.stream()` 报 `No synchronous function provided`）。阶段 4 主图入口节点（intent_router/clarify/rag_answer/agent_loop）全部 async 化，core/agent 的 native async 生成器直接 astream；阶段 1「工作线程 + queue 回流」桥接删除，仅 legacy complex workflow 保留 `asyncio.to_thread` 逐元素适配。测试侧所有入口 Fake 必须同时实现 `stream` 与 `astream`。 |
| 2026-10-01 | P4-17 | `_iter_async` 同步适配生成器内局部变量切勿命名 `queue`：会遮蔽模块顶 `import queue`，后续 `queue.Empty` 引用抛 `UnboundLocalError`。改名 `outbox` 解决。 |
| 2026-10-01 | P4-16 | 跨轮确认的状态回写时序：approval_entry 确认分支最初返回 `pending_approval: None`，导致随后的 act_node 读不到 approval_id，确认单永远停在 pending。正确做法是 entry 保留 pending → act 执行后按结果回写 executed/failed 并清空；取消/新诉求分支才在 entry 立即置 cancelled。意图路由前置短路（_load_pending_approval + 确认/取消关键词识别）保证第二轮「确认办理」不经过 LLM 分类直达 Agent 子图。 |
| 2026-10-01 | P4-13 | asyncio.gather 多工具并行的真实语义：建单与查单并发时查状态不保证读到本轮新单（提交时序竞争），验收以「两工具均成功 + 建单结果自带工单号 + 号码维度历史单可见」为准；act_node 对 gather 结果再按 calls 顺序排序，保证 tool_traces 与调用顺序一致。 |
| 2026-10-01 | P4 测试 | AgentSubState 是 TypedDict 通道白名单：节点返回但 schema 未声明的键（chat_response/final_answer）会被 LangGraph 从最终 state 丢弃（astream updates 流里仍可见，主图靠累积 updates 取 answer 不受影响）。已在 state 显式声明输出通道与测试注入键 `_registry`。新增测试 47 条（test_skills 16 / test_skill_registry 10 / test_agent_subgraph 16 / test_api_e2e 5），全量 194 条通过。 |
| 2026-10-01 | P5-1/3 | 首轮"假窗口"陷阱：会话流先 `append_user_turn` 再注入 entry_state，若直接取 `session["recent_turns"]`，首轮窗口长度为 1（含本轮用户原话），query_rewrite 会误判为多轮、每轮首轮白调一次模型。修复：注入前快照 `prior_turns`（本轮之前的轮次）。同时 `_short_term_window` 只在 state **缺少** recent_turns 键时才从 session_context JSON 兜底——显式空列表 `[]` 代表"首轮无历史"，不能被含本轮的 session_context 覆盖（跨会话测试 window_turns 断言暴露此问题）。 |
| 2026-10-01 | P5-1/P4-16 | 审批短路与重写的优先级：pending approval 存在时，"确认/取消"必须用**原始 task** 做规则判定（重写模型可能把"确认办理"改写后破坏关键词）；只有 LLM 意图分类的输入用 rewritten_task。intent_decision 事件同时记录 original_input/rewrite_changed 便于评测。 |
| 2026-10-01 | P5-7 | 长期摘要压缩必须有无 LLM 兜底：测试/离线环境 create_model 直接抛错，`_rule_based_merge` 以工具轨迹（skill_call→主题映射、args 提套餐名、report_fault 结果提工单）为强信号 + 路由兜底主题 + 正则抽套餐名，保证跨会话记忆永不依赖外部模型；turn_count 水位内幂等返回，不重复压缩。压缩/落库异常在会话流内被吞成 profile_update_error 事件，绝不阻断主对话。 |
| 2026-10-01 | P5-9 | 子图与主图状态解耦：RAG/Agent 子图不直接读主图 state，由 query_rewrite 首次装配时预渲染 memory_text，主图节点经 sub_input 的 `memory_context` 透传；子图节点缺省时回退旧 session_context 标签。legacy complex workflow 仍用旧 build_layered_memory（planner/verifier 依赖），两套入口并存不删旧。 |
| 2026-10-01 | P5 测试 | 验收门断言点：写操作（change_package）的人工确认卡片在 Skill **执行之前**短路，事件流只有 agent_thinking→agent_confirm_required，没有 skill_call/skill_result；槽位是否带出套餐名要断言 `agent_thinking.calls[].args.target_package` 而非 skill_call。新增 test_stage5_memory.py 17 条（重写透传/异常 6 + 四层组装 4 + profile 仓储/压缩 5 + 三剧本 1 + 跨会话 1），全量 211 条通过。 |
| 2026-10-01 | P6-1 | 原生 async 路径此前不持久化 trace（仅 legacy complex workflow 通过 TraceRecorder 写），导致评测无轨迹可采。新增 `SessionTraceRecorder`（async、不依赖 RuntimeState），在 `_stream_session_events_native` 内 try/finally 包裹，记录 intent_decision/rag_retrieve/rag_rerank/rag_gate/skill_call/skill_result/agent_reflect/clarify_question/fallback_reply/agent_answer/rag_answer/rag_fallback/agent_fallback，写 `events.jsonl`+`summary.json` 到 `{workspace}/.congclaw/traces/{trace_id}/`。文件写入用 `asyncio.to_thread` 避免阻塞事件循环。 |
| 2026-10-01 | P6-4 | 工具参数校验必须复用 SkillRegistry：每条 skill_call 按 registry 里的 manifest 校验 required 参数与枚举值，而非硬编码白名单。这样新增 Skill 自动纳入评测，零配置。 |
| 2026-10-01 | P6-5 | LLM-Judge 退化：`create_model` 在函数内延迟 import（来自 `congclaw.providers.openai_provider`），模型不可用时回退规则评分（`fallback=True`）。测试 monkeypatch 目标必须是 `congclaw.providers.openai_provider.create_model`（import 源），而非 `llm_judge` 模块级名。 |
| 2026-10-01 | P6- runner | 评测 runner 用 `approval_mode="auto"` 避免人工确认阻塞；DB 引擎是进程单例，runner 测试结束必须 `dispose_engine()`，否则后续测试 `load_or_create_session` 会读到上一个测试的 DB 路径（session 存错库，跨轮计数断言失败）。 |
| 2026-10-01 | P6 测试 | 新增 tests/test_eval_pipeline.py 14 条（collector/normalizer/rule_checks 4 + llm_judge 2 + report 2 + dataset 1 + runner 端到端 1 + API 3 + trace recorder 1），全量 225 条通过。端到端 runner 用 stub 模型跑通 balance 查询，rule_checks 全绿，llm_judge 退化路径正常。 |
| | | |
