# 电信客服智能体改造 · TodoList 清单

> 依据 [PRD-电信客服智能体.md](./PRD-电信客服智能体.md) 拆解。每个任务可独立勾选，每个阶段结束有"验收门"，验收门通过后才进入下一阶段。
> 勾选方式：把 `[ ]` 改为 `[x]`。编号规则：P{阶段}-{序号}。

## 进度总览

| 阶段 | 主题 | 任务数 | 状态 |
| --- | --- | --- | --- |
| 阶段 0 | 基础设施与领域瘦身 | 12 | ✅ 完成 |
| 阶段 1 | FastAPI 服务化改造 | 9 | ⬜ 未开始 |
| 阶段 2 | 意图识别与任务调度 | 10 | ⬜ 未开始 |
| 阶段 3 | RAG 检索子图（LangGraph） | 22 | ⬜ 未开始 |
| 阶段 4 | Skill 工具集 + Agent 推理引擎 | 16 | ⬜ 未开始 |
| 阶段 5 | 多轮记忆与查询重写 | 9 | ⬜ 未开始 |
| 阶段 6 | 自动化评测流水线 | 8 | ⬜ 未开始 |
| 阶段 7 | 收尾与演示打磨 | 4 | ⬜ 未开始 |
| **合计** | | **90** | |

状态约定：⬜ 未开始 / 🟦 进行中 / ✅ 完成（整阶段任务全部勾选且验收门通过才标 ✅）

---

## 阶段 0：基础设施与领域瘦身

**目标**：把"代码 Agent"清空为干净的客服骨架，装好新基础设施（SQLite + Milvus Lite + 本地模型，零外部服务）。

- [x] **P0-1** 添加依赖：`uv add fastapi uvicorn docling pymilvus sqlalchemy aiosqlite rank_bm25 sentence-transformers`
- [x] **P0-2** 删除 `src/mokioclaw/agents/`（search_agent.py、code_agent.py）及所有引用
- [x] **P0-3** 删除代码类工具：`tools/bash_tool.py`、`file_tools.py`、`grep_tool.py`、`notepad_tool.py`、`web_search_tool.py`
- [x] **P0-4** 清空 [tools/registry.py](./src/mokioclaw/tools/registry.py) 中对已删工具的注册，保证 import 不报错
- [x] **P0-5** 清理 prompts/stage3、stage4 中 planner/verifier 的代码任务描述（先置空，阶段 2/4 重写）
- [x] **P0-6** 同步删除/跳过失效旧测试（test_tools、test_graph、test_cli_smoke 中引用已删模块的用例）
- [x] **P0-7** 新建 `src/mokioclaw/db/` 骨架：`__init__.py`、`engine.py`（aiosqlite 异步引擎，开启 WAL）、`schema.sql`
- [x] **P0-8** 应用启动时自动执行 schema.sql 初始化 `data/telecom_cs.db`（.env 配 DB_PATH）<!-- 阶段0提供 `uv run python -m mokioclaw.db` 幂等建库；FastAPI 生命周期自动调用在 P1-1 挂载 -->
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

- [ ] **P1-1** 新建 `api/main.py`：FastAPI app、生命周期事件（建库、模型预热占位）、健康检查 `/health`
- [ ] **P1-2** 新建 `api/routes/chat.py`：`POST /api/v1/chat`，SSE（text/event-stream）流式返回
- [ ] **P1-3** 新建 `api/deps.py`：依赖注入（DB session、graph 实例、配置）
- [ ] **P1-4** 把 [core/agent.py](./src/mokioclaw/core/agent.py) 的 `stream_session_events()` 包成 async 生成器，事件转 SSE 格式
- [ ] **P1-5** 过渡期用 `asyncio.to_thread` 包裹同步 LLM/工具调用（阶段 4 移除）
- [ ] **P1-6** 建 SQLite `session` 表模型（session_id、轮次、当前意图、待确认槽位、消息 JSON、创建/更新时间）
- [ ] **P1-7** 改造 [core/session.py](./src/mokioclaw/core/session.py)：load/save 从 session.json 切到 SQLite，接口签名不变
- [ ] **P1-8** 改造 [cli/app.py](./src/mokioclaw/cli/app.py)：改为 HTTP/SSE 客户端请求 `/chat`
- [ ] **P1-9** 改造 TUI（cli/tui/）：SSE 流式渲染，审批交互改为后续确认消息（本阶段可先屏蔽）

**🚪 阶段 1 验收门**
- [ ] `uvicorn mokioclaw.api.main:app` 启动成功
- [ ] `mokioclaw "你好"` 经 HTTP 拿到流式回复
- [ ] 重启服务后历史会话不丢失（SQLite 持久化验证）

---

## 阶段 2：意图识别与任务调度

**目标**：五路意图判定 + 追问澄清（≤5 轮）+ 兜底，用占位引擎先跑通对话管控。

- [ ] **P2-1** 扩展 [graph/state.py](./src/mokioclaw/graph/state.py)：`intent_category`、`clarify_count`、`pending_slots`、`fallback_reason`、`unknown_count`
- [ ] **P2-2** 写意图识别 prompt：四类业务定义 + 正/反例，输出结构化 JSON（类别、置信度、理由、待补槽位）
- [ ] **P2-3** 改造 `intent_router_node`：输出 rag_query / agent_service / clarify / irrelevant / unknown 五类
- [ ] **P2-4** 新增 `clarify_node`：按 pending_slots 生成追问话术，`clarify_count += 1`
- [ ] **P2-5** 新增 `fallback_node`：兜底话术（说明服务边界 + 引导回四类业务），记录 fallback_reason
- [ ] **P2-6** 强制兜底逻辑：`clarify_count >= 5` 或连续 unknown 达阈值 → 直接路由 fallback
- [ ] **P2-7** 重排 [graph/workflow.py](./src/mokioclaw/graph/workflow.py)：intent_router 条件边 → rag_answer / agent_loop / clarify / fallback
- [ ] **P2-8** `rag_answer` 占位节点：回复"知识库建设中"
- [ ] **P2-9** `agent_loop` 占位节点：回复"转人工预处理中"
- [ ] **P2-10** 编写 10 条意图测试输入（五类各覆盖，含模糊与无关）

**🚪 阶段 2 验收门**
- [ ] 10 条测试输入分流全部正确
- [ ] 模糊输入连续追问第 5 轮后自动兜底
- [ ] 无关请求（如"写首诗"）直接兜底

---

## 阶段 3：RAG 快速检索引擎（LangGraph 子图）

**目标**：按 PRD 3.3 节实现 `rag_subgraph`，全流程图节点化，替换阶段 2 占位。

### 3A 数据与离线入库
- [ ] **P3-1** 新建 `rag/state.py`：`RagSubState`（query / rewritten_query / bm25_hits / dense_hits / fused_hits / reranked_hits / evidence / answer / sources / rag_attempts）
- [ ] **P3-2** SQLite 建 `chunk_meta` 表（child_id、parent_id、doc_source、position、child_text）
- [ ] **P3-3** 建 Milvus collection（child 粒度向量，含 child_id 标量字段）
- [ ] **P3-4** `rag/parsing.py`：Docling 解析 PDF/Word/HTML 异构文档
- [ ] **P3-5** 句子边界切分（中文标点/规则分句），生成 Child 细粒度切片（1~3 句）
- [ ] **P3-6** 生成 Parent 段落块并维护 child→parent 映射
- [ ] **P3-7** 封装 BGE-M3 embedding 异步接口（懒加载、CPU 推理）
- [ ] **P3-8** `rag/ingest.py`：向量化入 Milvus Lite + 元信息/父子映射写 SQLite
- [ ] **P3-9** 实现 `POST /api/v1/knowledge/ingest` 接口
- [ ] **P3-10** 准备 5~10 份电信知识文档（套餐说明/资费规则/宽带 FAQ）放 `knowledge/` 并完成入库

### 3B 检索子图节点
- [ ] **P3-11** `retrieve_bm25` 节点（rank_bm25 关键词召回，async）
- [ ] **P3-12** `retrieve_dense` 节点（Milvus 稠密语义召回，async）
- [ ] **P3-13** `rrf_fusion` 节点：等待两路齐备，RRF 融合排序
- [ ] **P3-14** `rerank` 节点：Cross-Encoder 精排取 Top-K
- [ ] **P3-15** `evidence_gate` 节点 + 条件路由：有命中 / 空或低置信
- [ ] **P3-16** `parent_lookup` 节点：回溯父分片 + 前后邻域 child 扩展 + 去重合并
- [ ] **P3-17** `rewrite_once` 节点：放宽重写 1 次（`rag_attempts` 计数，超限走兜底）
- [ ] **P3-18** `generate` 节点：四层组装提示词（系统→记忆→证据→问题）生成答案，带来源编号
- [ ] **P3-19** `rag_fallback` 节点：无证据话术并回传 fallback 原因

### 3C 组装与接线
- [ ] **P3-20** `rag/workflow.py` 的 `build_rag_subgraph()`：START 两条出边并行召回 → fusion → rerank → gate 条件边 → parent_lookup → generate / rewrite_once / rag_fallback
- [ ] **P3-21** 主图 [graph/workflow.py](./src/mokioclaw/graph/workflow.py) 用编译后的子图替换 rag_answer 占位（子图作为 node 挂载，问句进/答案+来源出）
- [ ] **P3-22** 各节点中间结果（两路命中、融合分、精排分、证据）写入 trace

**🚪 阶段 3 验收门**
- [ ] 20 条业务问题答案命中正确文档片段
- [ ] 父子回溯后上下文完整、无断句
- [ ] trace 可见完整子图节点序列与并行召回事件
- [ ] 知识库外问题走通 `rewrite_once → rag_fallback`
- [ ] 单轮检索 P95 < 2s（本地模型）

---

## 阶段 4：电信 Skill 工具集 + Agent 深度推理引擎

**目标**：agent_subgraph 上线替换占位；六个 Skill 可用；热插拔生效；全链路原生 async。

### 4A Skill 框架与工具
- [ ] **P4-1** 新建 `skills/base.py`：Skill 基类（name / description / 参数 schema / async run）
- [ ] **P4-2** `query_balance`：余额/实时话费查询（只读）
- [ ] **P4-3** `query_package`：当前套餐与余量查询（只读）
- [ ] **P4-4** `list_packages`：可办理套餐列表（只读）
- [ ] **P4-5** `report_fault`：故障报修建单（写）
- [ ] **P4-6** `query_fault_status`：工单进度查询（只读）
- [ ] **P4-7** `change_package`：套餐变更办理（写，需人工确认）
- [ ] **P4-8** SQLite 建模拟业务表 + 种子数据：`account`、`user_package`、`package_catalog`、`fault_ticket`
- [ ] **P4-9** `skills/registry.py`：动态注册中心（扫描目录、元信息注册、按名取用）
- [ ] **P4-10** 目录监听热加载：运行中增删 Skill 文件即生效，无需重启
- [ ] **P4-11** Skill 管理接口：`GET/POST/DELETE /api/v1/skills`

### 4B Agent 推理子图
- [ ] **P4-12** 思考节点（planner 改造）：客服域 prompt，分析对话状态决定调用哪些 Skill
- [ ] **P4-13** 工具编排节点：单轮支持多工具并行（asyncio.gather）
- [ ] **P4-14** 反思校验节点（verifier 改造）：校验工具结果业务一致性（如办理状态异常检测）
- [ ] **P4-15** 失败计数 `attempts`：异常/校验失败超阈值走兜底话术
- [ ] **P4-16** `change_package` 接入现有 approval 机制：办理前推确认，用户确认后才执行
- [ ] **P4-17** 移除阶段 1 的 `asyncio.to_thread` 过渡代码，全部改为原生 async

**🚪 阶段 4 验收门**
- [ ] 查余额链路走通（单工具）
- [ ] 宽带报修走通（建单 + 查状态，多工具）
- [ ] 套餐变更走通（含人工确认卡片）
- [ ] 运行中新增测试 Skill，不重启即可被发现和调用

---

## 阶段 5：多轮对话记忆与查询重写

**目标**：指代消解生效，分层记忆完整，跨会话可恢复用户摘要。

- [ ] **P5-1** 新增 `query_rewrite_node`，置于 intent_router 之前
- [ ] **P5-2** 重写 prompt：输入短期窗口 + 当前问题，输出独立完整问句
- [ ] **P5-3** 无指代/省略时原样透传（prompt 判定 + 单测覆盖）
- [ ] **P5-4** 改造 [graph/memory.py](./src/mokioclaw/graph/memory.py)：working 层 = 短期会话窗口 + 当前意图 + 待确认槽位
- [ ] **P5-5** history 层 = 长期用户摘要（历史咨询主题、在办工单、偏好套餐）；新增检索证据层
- [ ] **P5-6** SQLite 建 `user_profile` 表
- [ ] **P5-7** 会话结束时由压缩机制生成长期摘要并落库
- [ ] **P5-8** 新会话开始时装载对应用户的长期摘要
- [ ] **P5-9** 统一四层提示词组装入口（系统提示词 → 记忆 → 证据 → 用户问题），各节点共用

**🚪 阶段 5 验收门**
- [ ] 剧本"查 5G 套餐 → 那这个多少钱 → 帮我办这个"：第 2 轮指代正确重写；第 3 轮路由到办理 Agent 且槽位自动带出套餐名
- [ ] 跨会话重开后能记起历史咨询主题

---

## 阶段 6：自动化评测流水线

**目标**：一键跑评测，自动输出含规则校验与 LLM-Judge 的报告。

- [ ] **P6-1** 扩展 [core/trace.py](./src/mokioclaw/core/trace.py)：记录意图判定、RAG 召回 Top-K、工具入参出参、反思校验结论
- [ ] **P6-2** `eval/collector.py`：从 trace 采集会话轨迹
- [ ] **P6-3** `eval/normalizer.py`：归一化为标准评测样本（输入/期望/实际轨迹/工具调用序列）
- [ ] **P6-4** `eval/rule_checks.py`：分流正确性、工具参数合法性、兜底触发、追问 ≤5 轮断言
- [ ] **P6-5** `eval/llm_judge.py`：答案质量、推理逻辑、话术合规性 1~5 分 + 评语
- [ ] **P6-6** `eval/report.py`：生成 Markdown 报告（通过率、分项得分、失败归因、轨迹回链）到 `eval/reports/`
- [ ] **P6-7** 准备 30 条标注评测集（四类业务 + 模糊 + 无关）
- [ ] **P6-8** 接口：`POST /api/v1/eval/run`、`GET /api/v1/eval/reports/{id}`

**🚪 阶段 6 验收门**
- [ ] 评测集一键跑完自动出报告
- [ ] 报告含通过率、LLM-Judge 分项得分、失败用例归因
- [ ] 每条失败用例可回链原始轨迹

---

## 阶段 7：收尾与演示打磨

- [ ] **P7-1** 更新 README：新架构图、四类业务演示剧本、启动命令、环境变量说明
- [ ] **P7-2** 补齐单测：意图分流、RRF 融合、父子回溯、Skill 热插拔、评测规则
- [ ] **P7-3** 完整演示彩排：uvicorn 起服务 → CLI 演示四类业务 → 热插拔 Skill → 跑评测出报告
- [ ] **P7-4** `uv run pytest -q` 全绿

---

## 总验收 Checklist（对应 PRD 第 8 节）

- [ ] 1. 四类业务（余额、套餐、故障、办理）多轮对话端到端走通，意图分流正确
- [ ] 2. 模糊意图追问 ≤5 轮后兜底；无关请求直接兜底
- [ ] 3. RAG 答案附来源片段，父子分片回溯生效
- [ ] 4. Agent 链路有完整"思考→调用工具→反思校验"轨迹
- [ ] 5. Skill 热插拔：运行中增删工具目录，不重启即生效
- [ ] 6. 指代消解：多轮指代问题被正确重写并路由
- [ ] 7. 评测流水线一键运行，自动输出含规则校验 + LLM-Judge 的报告
- [ ] 8. `uv run pytest -q` 全绿

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
| | | |
