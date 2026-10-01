# PRD：电信客服业务智能体（基于 MokioClaw 改造）

| 项 | 内容 |
| --- | --- |
| 文档版本 | v1.0 |
| 编写日期 | 2026-10-01 |
| 文档定位 | 学习/面试项目改造规划：现状差距 → 目标架构 → 分阶段改造步骤 → 每步验收标准 |
| 基线代码 | MokioClaw（教学向 Mini CodeAgent，LangChain + LangGraph + Typer CLI + Textual TUI） |
| 目标产品 | 面向电信客服高频场景的业务智能体（FastAPI + LangGraph，RAG 快查 + Agent 深推理双引擎） |

---

## 1. 项目背景与目标

### 1.1 背景

现有 MokioClaw 是一个"写代码"的通用 MultiAgent：planner 分派 searchAgent / codeAgent，verifier 验收，配套 CLI/TUI 交互、分层记忆、上下文压缩、checkpoint、trace。它的骨架（LangGraph 编排、意图路由雏形、分层记忆、工具注册、验收反思、链路观测）与电信客服智能体高度同构，但领域内容完全不同。

本项目在**保留骨架、替换血肉**的思路下，把 MokioClaw 改造为电信客服智能体：面向余额查询、套餐咨询、故障报修、业务办理四类高频场景，支撑多轮复杂对话。

### 1.2 目标

1. 双查询引擎：**RAG 快速检索**（保响应速度）+ **Agent 深度推理**（保复杂业务处理能力），由意图识别统一分流。
2. 多轮对话能力：会话状态管理、长短期记忆、指代消解与查询重写、分层组装提示词。
3. 对话管控：意图模糊最多 5 轮追问澄清；无关请求或多次识别失败自动兜底。
4. 工程能力：FastAPI + async/await 异步工具调度；Skill 工具动态注册与热插拔；自动化评测流水线。

### 1.3 非目标（本期不做）

- 不做真实运营商 BOSS/CRM 系统对接，业务工具用模拟数据实现。
- 不做前端对话页面，对外只提供 HTTP/SSE 接口 + 改造后的 CLI 演示入口。
- 不做模型微调，意图识别与查询重写均基于提示词工程。
- 不做多租户、权限体系、生产级高可用部署。

---

## 2. 现状盘点：MokioClaw 资产映射

改造的核心原则：**能复用的不重写，该删除的不保留**。下表是现有代码与新系统的映射关系，后续所有改造步骤都以此表为索引。

| 现有模块 | 文件 | 处置 | 映射到新系统 |
| --- | --- | --- | --- |
| LangGraph 工作流组装 | [workflow.py](file:///e:/sjc/work/2026.08.31中期检查/其他/agent-dian001/MokioAgent-master/src/mokioclaw/graph/workflow.py) | **改造** | 重排为 `intent_router → rag_answer / agent_loop / clarify / fallback` 的客服对话图 |
| 意图路由（chat/workflow 二路） | [nodes.py](file:///e:/sjc/work/2026.08.31中期检查/其他/agent-dian001/MokioAgent-master/src/mokioclaw/graph/nodes.py) 中 `intent_router_node` | **改造** | 扩展为四路分流：RAG / Agent / 追问澄清 / 兜底，加追问计数 |
| 分层记忆（rules / working / history） | [memory.py](file:///e:/sjc/work/2026.08.31中期检查/其他/agent-dian001/MokioAgent-master/src/mokioclaw/graph/memory.py) | **改造** | 变为客服版三层：系统提示词层 / 短期会话窗口 / 长期用户摘要，加检索证据层 |
| 会话管理（session.json 文件） | [session.py](file:///e:/sjc/work/2026.08.31中期检查/其他/agent-dian001/MokioAgent-master/src/mokioclaw/core/session.py) | **改造** | 从本地文件迁移到 SQLite 会话表，支撑服务化持久化 |
| Graph state 定义 | [state.py](file:///e:/sjc/work/2026.08.31中期检查/其他/agent-dian001/MokioAgent-master/src/mokioclaw/graph/state.py) | **改造** | 字段换成客服域：intent、clarify_count、retrieval_evidence、tool_traces 等 |
| 上下文压缩 | nodes.py 中 `context_compressor_node` | **保留** | 逻辑不变，压缩对象从"代码任务 transcript"变为"多轮对话 transcript" |
| 工具注册表（静态列表） | [registry.py](file:///e:/sjc/work/2026.08.31中期检查/其他/agent-dian001/MokioAgent-master/src/mokioclaw/tools/registry.py) | **改造** | 升级为 Skill 注册中心：动态注册、热插拔加载 |
| verifier 验收节点 | nodes.py 中 `verifier_node` | **改造** | 变成 Agent 引擎内的"结果反思校验"节点 |
| Trace 链路观测 | [trace.py](file:///e:/sjc/work/2026.08.31中期检查/其他/agent-dian001/MokioAgent-master/src/mokioclaw/core/trace.py) | **保留扩展** | 作为评测流水线的"轨迹采集"数据源 |
| CLI / Textual TUI | [app.py](file:///e:/sjc/work/2026.08.31中期检查/其他/agent-dian001/MokioAgent-master/src/mokioclaw/cli/app.py)、tui/ | **改造** | 不再直连本地 graph，改为调用 FastAPI 接口的演示客户端 |
| searchAgent / codeAgent | agents/ | **删除** | 由"电信业务 Agent"取代，文件操作族工具随之退场 |
| Bash / File / Grep / Notepad 工具 | tools/ 下 bash_tool、file_tools、grep_tool、notepad_tool | **删除** | 客服 Agent 不需要操作本地文件与命令行 |
| Tavily WebSearch 工具 | tools/web_search_tool.py | **替换** | 由 RAG 检索工具取代（知识库私有化，不搜公网） |
| checkpoint / approval | core/checkpoint.py、core/approval.py | **保留（降优先级）** | 高危业务办理（如变更套餐）复用 approval 机制做人工确认 |
| —（新增） | api/、rag/、skills/、eval/、db/ | **新增** | FastAPI 层、检索链路、电信工具集、评测流水线、元数据库 |

**命名约定**：Python 包名保持 `mokioclaw` 不变（避免大规模 import 重命名），产品对外名称为"电信客服智能体"，CLI 命令保留 `mokioclaw` 作为演示入口。

---

## 3. 目标系统架构

### 3.1 整体架构

```text
┌─────────────────────────────────────────────────────────────┐
│  接入层                                                       │
│  CLI/TUI 演示客户端  ──HTTP/SSE──>  FastAPI 服务               │
│                                    /chat  /sessions  /skills  │
├─────────────────────────────────────────────────────────────┤
│  编排层（LangGraph，主图）                                     │
│  query_rewrite → intent_router ──┬── rag_subgraph（RAG 快查）  │
│       ↑      （每个分支都是可编译子图）├── agent_subgraph（深度推理）│
│       │                          ├── clarify（追问，≤5 轮）    │
│  会话状态/记忆读写               └── fallback（兜底）           │
├─────────────────────────────────────────────────────────────┤
│  能力层（均以 LangGraph 子图节点实现）                          │
│  RAG 检索子图                  │  Skill 工具集（热插拔）        │
│  Docling 解析→父子分片→入库     │  余额/套餐/故障/办理/工单      │
│  BM25 ∥ 稠密向量 → RRF 融合    │  动态注册中心                  │
│  → Cross-Encoder 精排           │  思考/工具编排/反思节点        │
│  → 父分片回溯+邻域合并          │                              │
├─────────────────────────────────────────────────────────────┤
│  数据层（本地轻量方案）                                         │
│  Milvus Lite（向量）  SQLite（分片元信息/会话/工单/工具注册表）  │
│  BGE-M3（本地加载）   Cross-Encoder（本地加载）                 │
├─────────────────────────────────────────────────────────────┤
│  评测层                                                       │
│  轨迹采集 → 数据归一化 → 规则校验 → LLM-Judge → 评测报告        │
└─────────────────────────────────────────────────────────────┘
```

### 3.2 双引擎分工

| | RAG 快速检索引擎 | Agent 深度推理引擎 |
| --- | --- | --- |
| 承接意图 | 套餐咨询、资费规则、业务知识问答 | 余额查询、故障报修、业务办理 |
| 实现形态 | **LangGraph 子图**（`rag_subgraph`），每一步检索/融合/精排/回溯/生成都是图节点 | **LangGraph 子图**（`agent_subgraph`），思考/工具编排/反思为图节点 |
| 依赖 | 知识库检索链路 | Skill 工具集 |
| 复用基线 | 新增为主，子图编译方式参考 [workflow.py](file:///e:/sjc/work/2026.08.31中期检查/其他/agent-dian001/MokioAgent-master/src/mokioclaw/graph/workflow.py) | planner→工具→verifier 循环改造 |

### 3.3 RAG 检索子图设计（LangGraph 实现）

RAG **不是**在 `rag_answer` 节点里用普通函数顺序调用，而是独立编译成一个 LangGraph `StateGraph` 子图，由主图意图路由条件边进入。这样做的收益：每一步可独立观测（天然产出评测轨迹）、并行召回用图原生并发表达、空结果/低置信度可用条件边回退而不是在业务代码里写 if-else。

```text
                    ┌──────────────── rag_subgraph ─────────────────┐
重写后的独立问句 ──> │ retrieve_bm25 ──┐                              │
                    │                 ├─(并行 fan-out, asyncio)      │
                    │ retrieve_dense ─┘                              │
                    │        ↓                                      │
                    │ rrf_fusion（两路结果 RRF 融合）                  │
                    │        ↓                                      │
                    │ rerank（Cross-Encoder 精排，取 Top-K）          │
                    │        ↓                                      │
                    │ evidence_gate（条件边）                         │
                    │   ├─ 有命中 → parent_lookup（回溯父分片+        │
                    │   │             邻域扩展+去重合并）→ generate   │
                    │   └─ 空/低置信 → rewrite_once（放宽重写1次）    │
                    │                   └─ 仍无命中 → rag_fallback   │
                    └────────────────────────────────────────────────┘
                                      ↓ 答案+来源片段 / 兜底原因
                              回到主图 final 输出
```

**子图状态（`RagSubState`）**：独立 TypedDict，包含 `query / rewritten_query / bm25_hits / dense_hits / fused_hits / reranked_hits / evidence / answer / sources / rag_attempts`；主图只把"重写后的问句"传入、把"答案 + 来源"接回，主从状态解耦。

**关键图表达**：
- 并行召回：`START → retrieve_bm25` 与 `START → retrieve_dense` 两条出边（或 `Send` API fan-out），两节点均为 async 节点，图调度器并发执行，天然等价于 `asyncio.gather`。
- 条件回退：`evidence_gate` 用 `add_conditional_edges` 路由到生成 / 放宽重写 / `rag_fallback`；重写最多 1 次（`rag_attempts` 计数），避免死循环。
- 子图接入：`build_rag_subgraph()` 编译产物作为主图的一个 node 挂载（或整体作为条件边目标），与 [build_entry_workflow / build_complex_workflow](file:///e:/sjc/work/2026.08.31中期检查/其他/agent-dian001/MokioAgent-master/src/mokioclaw/graph/workflow.py) 的"入口图 + 复杂图"分层模式保持一致。

---

## 4. 功能需求

### F1 多轮对话与会话状态管理

- 基于 LangGraph 管理会话状态；会话元数据（session_id、轮次、当前意图、待确认槽位）持久化到 SQLite，服务重启可恢复。
- 长短期记忆维护：
  - **短期记忆**：当前会话最近 N 轮原始消息（滑动窗口）。
  - **长期记忆**：跨会话的用户摘要（历史咨询主题、在办工单、偏好套餐），复用现有 `history_summary` 压缩机制生成，存 SQLite。
- 指代消解与查询重写：`query_rewrite` 节点把"那这个多少钱？"这类指代/省略输入，结合会话历史重写为完整独立问句，再进入意图识别。
- 分层组装提示词：每个节点按 **系统提示词 → 记忆信息（短期窗口+长期摘要）→ 检索证据 → 用户问题** 四层组装，复用 [memory.py](file:///e:/sjc/work/2026.08.31中期检查/其他/agent-dian001/MokioAgent-master/src/mokioclaw/graph/memory.py) 的 `build_layered_memory` 思路，把 `working_memory` 换成客服域字段。
- 后端工具调度全部 async/await：检索、工具调用、LLM 调用均为异步，FastAPI 单进程可支撑多会话并发。

### F2 意图识别与任务调度

意图分流规则（在现有 `intent_router_node` 上扩展）：

| 意图类别 | 示例 | 去向 |
| --- | --- | --- |
| 基础业务查询 | "5G 畅享套餐包含多少流量" | RAG 引擎 |
| 故障报修 | "我家宽带从昨天开始断网" | Agent 引擎 |
| 业务办理 | "帮我把套餐升级成 199 档" | Agent 引擎（含人工确认） |
| 余额/账单查询 | "我还有多少钱话费" | Agent 引擎（调工具） |
| 意图模糊 | "我想换个更划算的" | 追问澄清，**最多 5 轮**，第 5 轮仍不清则兜底 |
| 无关请求 | "帮我写首诗" | 直接兜底回复 |
| 多次识别失败 | 连续无法归类 | 自动兜底回复 |

- 兜底回复：礼貌说明服务边界，引导用户回到四类业务。
- 追问计数 `clarify_count` 写入会话状态，跨轮累计，达到 5 立即路由 fallback。

### F3 RAG 快速检索引擎（LangGraph 子图实现）

RAG 链路以独立 LangGraph 子图 `rag_subgraph` 实现（见 3.3 节），检索全流程拆成图节点，主图只与子图做"问句进、答案出"的交互。

- **文档解析**：Docling 统一解析异构文档（PDF 套餐说明 / Word 业务规范 / HTML 帮助中心），输出结构化文本（离线入库侧，不属于在线子图）。
- **分片策略**：按句子边界切分，采用 Parent-Child 父子分片——Child 细粒度切片（1~3 句）保障召回精度，Parent 段落块保障生成上下文完整。
- **入库**：BGE-M3 本地生成向量存 Milvus Lite；SQLite 管理托管分片与父子映射元信息（child_id → parent_id、文档来源、位置）。
- **子图节点职责**：
  - `retrieve_bm25` / `retrieve_dense`：两条 START 出边触发，图调度器**并行执行**关键词检索与稠密语义检索，无需手写 asyncio.gather。
  - `rrf_fusion`：等待两路召回齐备，RRF 融合排序。
  - `rerank`：Cross-Encoder 精排取 Top-K。
  - `evidence_gate`：条件路由节点——有命中进入 `parent_lookup`；空/低置信进入 `rewrite_once` 放宽重写（最多 1 次），仍无命中走 `rag_fallback`。
  - `parent_lookup`：命中子分片回溯父分片 + 邻域扩展（前后相邻 child）+ 合并去重，消除语义断裂。
  - `generate`：基于四层组装（系统提示词 → 记忆 → 检索证据 → 问题）生成答案，响应附带来源片段。
- 独立子图状态 `RagSubState`，与主图状态解耦；每个节点的中间结果（两路命中、融合分、精排分、证据）自动进入 trace，直接作为评测流水线的轨迹数据。

### F4 Agent 深度推理引擎

- 复用现有 `planner → 工具调用 → verifier` 循环骨架，改造为客服 Agent 的 **思考推理 → 工具编排调用 → 结果反思校验** 闭环：
  - 思考：分析当前对话状态与目标，决定下一步调哪个 Skill。
  - 编排：支持一次思考调用多个工具（如报修时同时创建工单 + 查询网点）。
  - 反思校验：对工具返回做一致性检查（如"办理成功但工单状态异常"则重试或转人工话术），由现有 `verifier_node` 改造。
- 失败处理：工具异常/校验失败计入 `attempts`，超过阈值走兜底话术（复用现有 max_attempts 机制）。

### F5 电信 Skill 工具集

- 首批工具（模拟数据，接口形态对齐真实 BOSS）：

| Skill | 说明 | 类型 |
| --- | --- | --- |
| query_balance | 余额/实时话费查询 | 只读 |
| query_package | 当前套餐与余量查询 | 只读 |
| list_packages | 可办理套餐列表 | 只读（可走 RAG 证据补充） |
| report_fault | 故障报修建单 | 写操作 |
| query_fault_status | 工单进度查询 | 只读 |
| change_package | 套餐变更办理 | 写操作，**需人工确认**（复用 approval 机制） |

- 工具注册中心：Skill 以目录约定 + 元信息描述文件组织，支持**动态注册与热插拔**——服务运行中增删 Skill 目录即可生效，无需重启（在 [registry.py](file:///e:/sjc/work/2026.08.31中期检查/其他/agent-dian001/MokioAgent-master/src/mokioclaw/tools/registry.py) 静态列表基础上升级为注册表 + 监听加载）。

### F6 自动化评测流水线

- **轨迹采集**：复用 [trace.py](file:///e:/sjc/work/2026.08.31中期检查/其他/agent-dian001/MokioAgent-master/src/mokioclaw/core/trace.py) 的 events.jsonl，补充记录 RAG 召回片段、工具入参出参、意图判定结果。
- **数据归一化**：把不同会话的轨迹统一成标准评测样本格式（输入、期望、实际轨迹、工具调用序列）。
- **规则校验**：硬规则断言——意图分流是否正确、工具调用参数是否合法、兜底是否触发、追问是否 ≤5 轮。
- **LLM-Judge**：对答案质量、推理逻辑、话术合规性打分（1~5 分 + 评语）。
- **报告输出**：自动生成 Markdown 评测报告（通过率、分项得分、失败用例归因），输出到 `eval/reports/`。

### F7 服务接口（FastAPI）

| 接口 | 方法 | 说明 |
| --- | --- | --- |
| `/api/v1/chat` | POST（SSE） | 多轮对话主入口，流式返回 |
| `/api/v1/sessions/{id}` | GET/DELETE | 会话查询与结束 |
| `/api/v1/skills` | GET/POST/DELETE | Skill 列表、热注册、热卸载 |
| `/api/v1/knowledge/ingest` | POST | 知识库文档解析入库 |
| `/api/v1/eval/run` | POST | 触发评测流水线 |
| `/api/v1/eval/reports/{id}` | GET | 获取评测报告 |

---

## 5. 技术选型（本地轻量方案）

| 能力 | 选型 | 说明 |
| --- | --- | --- |
| 服务框架 | FastAPI + uvicorn | 新增依赖 |
| 编排 | LangGraph（沿用） | 现有依赖不变 |
| LLM | 沿用现有 ChatOpenAI 兼容配置（.env 的 API_KEY/BASE_URL/MODEL） | 不变 |
| Embedding | BGE-M3，本地 sentence-transformers/FlagEmbedding 加载 | 新增 |
| 向量库 | Milvus Lite（pymilvus 本地文件模式，免部署服务端） | 新增 |
| 元数据/会话/工单 | SQLite 本地文件（SQLAlchemy + aiosqlite 异步驱动，零部署） | 新增 |
| 关键词检索 | rank_bm25 | 新增 |
| 精排 | bge-reranker（Cross-Encoder）本地加载 | 新增 |
| 文档解析 | docling | 新增 |
| 评测 | 自研流水线 + LLM-Judge（复用 LLM 配置） | 新增 |

---

## 6. 分阶段改造步骤（核心规划）

每个阶段都可独立运行、独立演示、独立验收；严格按依赖顺序推进。

### 阶段 0：基础设施与领域瘦身（前置）

**目标**：把项目从"代码 Agent"清空为干净的客服骨架，装好新基础设施。

步骤：
1. `uv add fastapi uvicorn docling pymilvus sqlalchemy aiosqlite rank_bm25 sentence-transformers`。
2. 删除领域不符代码：`agents/`（search/code agent）、`tools/bash_tool.py`、`file_tools.py`、`grep_tool.py`、`notepad_tool.py`、`web_search_tool.py`，同步清空 [registry.py](file:///e:/sjc/work/2026.08.31中期检查/其他/agent-dian001/MokioAgent-master/src/mokioclaw/tools/registry.py) 与 planner/verifier prompt 中的代码任务描述。
3. 初始化 SQLite 数据库文件 `data/telecom_cs.db`（建表脚本放 `db/schema.sql`，启动时自动执行）；验证 Milvus Lite 本地文件模式可写可读。
4. 下载 BGE-M3 与 bge-reranker 模型到本地 `models/` 目录，写最小加载冒烟脚本。

**验收**：`uv run pytest -q` 通过（删掉失效用例后）；冒烟脚本能输出一条向量和一个 rerank 分数。

### 阶段 1：FastAPI 服务化改造

**目标**：现有 graph 不变，先包成 HTTP 服务；CLI 改为客户端。

步骤：
1. 新增 `api/` 目录：`main.py`（FastAPI app）、`routes/chat.py`（`/chat` SSE 流式接口）、`deps.py`（依赖注入）。
2. 把 [core/agent.py](file:///e:/sjc/work/2026.08.31中期检查/其他/agent-dian001/MokioAgent-master/src/mokioclaw/core/agent.py) 的 `stream_session_events()` 包成 async 生成器，SSE 推送给前端/CLI；底层 LLM 与工具调用逐步改 async（本阶段先 `asyncio.to_thread` 包裹过渡，阶段 4 彻底异步化）。
3. 会话存储从 session.json 文件迁移到 SQLite `session` 表（改 [session.py](file:///e:/sjc/work/2026.08.31中期检查/其他/agent-dian001/MokioAgent-master/src/mokioclaw/core/session.py) 的 load/save 实现，接口签名不变）。
4. 改造 [cli/app.py](file:///e:/sjc/work/2026.08.31中期检查/其他/agent-dian001/MokioAgent-master/src/mokioclaw/cli/app.py) 与 TUI：不再本地起 graph，改为 HTTP/SSE 客户端请求 `/chat`。

**验收**：`uvicorn` 起服务后，`mokioclaw "你好"` 经 HTTP 拿到流式回复；重启服务后会话历史不丢失。

### 阶段 2：意图识别与任务调度

**目标**：四路分流 + 追问澄清 + 兜底，先用占位引擎跑通管控逻辑。

步骤：
1. 扩展 [state.py](file:///e:/sjc/work/2026.08.31中期检查/其他/agent-dian001/MokioAgent-master/src/mokioclaw/graph/state.py)：新增 `intent_category`、`clarify_count`、`pending_slots`、`fallback_reason` 字段。
2. 改造 `intent_router_node`：输出五类意图（rag_query / agent_service / clarify / irrelevant / unknown），写意图识别 prompt（含四类业务定义与示例）。
3. 新增 `clarify_node`（生成追问话术，`clarify_count+1`）与 `fallback_node`（兜底话术）；`clarify_count >= 5` 或连续 unknown 达阈值时强制 fallback。
4. 重排 [workflow.py](file:///e:/sjc/work/2026.08.31中期检查/其他/agent-dian001/MokioAgent-master/src/mokioclaw/graph/workflow.py)：`intent_router` 条件边到 `rag_answer`（占位）/`agent_loop`（占位）/`clarify`/`fallback`。
5. 占位引擎：rag_answer 先回"知识库建设中"，agent_loop 先回"转人工预处理"，保证链路通。

**验收**：构造 10 条测试输入覆盖五类意图，分流全部正确；模糊输入连续追问 5 轮后自动兜底；无关请求直接兜底。

### 阶段 3：RAG 快速检索引擎（LangGraph 子图）

**目标**：以 LangGraph 子图形式上线完整检索链路，替换阶段 2 的 rag 占位。

步骤：
1. 新增 `rag/` 目录：
   - 离线入库侧：`parsing.py`（Docling 解析 + 句子边界切分 + 父子分片）、`ingest.py`（BGE-M3 向量化入 Milvus Lite + SQLite 写父子映射元信息）；
   - 在线检索侧（**全部是子图节点，不放顺序脚本**）：`state.py`（`RagSubState` TypedDict）、`nodes.py`（`retrieve_bm25` / `retrieve_dense` / `rrf_fusion` / `rerank` / `evidence_gate` / `parent_lookup` / `rewrite_once` / `generate` / `rag_fallback` 节点）、`workflow.py`（`build_rag_subgraph()` 组装与编译）。
2. 在 SQLite 建表 `chunk_meta`（child_id、parent_id、doc_source、position）；建 Milvus collection（child 粒度向量）。
3. 准备 5~10 份电信知识文档（套餐说明、资费规则、宽带 FAQ）走 `/knowledge/ingest` 入库。
4. 在 [graph/workflow.py](file:///e:/sjc/work/2026.08.31中期检查/其他/agent-dian001/MokioAgent-master/src/mokioclaw/graph/workflow.py) 主图中把阶段 2 的 `rag_answer` 占位节点替换为 `build_rag_subgraph()` 编译产物（子图作为主图 node 挂载），主图传入重写后问句、接回答案与来源。
5. 图拓扑按 3.3 节实现：START 两条出边并行召回 → `rrf_fusion` → `rerank` → `evidence_gate` 条件边 → `parent_lookup` → `generate` / `rewrite_once`（`rag_attempts` 限 1 次）→ `rag_fallback`。
6. 所有检索节点实现为 **async 节点**，并发交给 LangGraph 调度器；各节点中间结果写入子图 state 并接入 trace。

**验收**：对"5G 套餐流量""宽带报修流程"等 20 条问题，答案命中正确文档片段；父子回溯后上下文完整无断句；trace 中可见完整子图节点序列与并行召回事件；构造一条知识库外问题验证 `rewrite_once → rag_fallback` 回退路径；单轮检索 P95 < 2s（本地模型）。

### 阶段 4：电信 Skill 工具集 + Agent 深度推理引擎

**目标**：agent_loop 上线，替换占位；Skill 支持热插拔；工具调用彻底异步化。

步骤：
1. 新增 `skills/` 目录：`base.py`（Skill 基类：name/description/schema/run）、六个业务 Skill（见 F5）、`registry.py`（动态注册中心，监听目录变化热加载）。
2. 在 SQLite 建模拟业务表：`account`（余额）、`user_package`、`package_catalog`、`fault_ticket`（工单）。
3. 改造 agent_loop：思考节点（planner 改造，prompt 换成客服域）→ 工具编排（支持单轮多工具并行 `asyncio.gather`）→ 反思校验节点（verifier 改造，校验工具结果与业务一致性）。
4. `change_package` 接入现有 approval 机制：办理前向用户推送确认卡片，确认后才执行。
5. 阶段 1 的 `asyncio.to_thread` 过渡代码全部替换为原生 async 工具实现。

**验收**：演示三条完整链路——查余额（单工具）、宽带报修（建单+查状态多工具）、套餐变更（含人工确认）；运行中向 `skills/` 目录新增一个测试 Skill，不重启服务即可被调用。

### 阶段 5：多轮对话记忆与查询重写

**目标**：补齐多轮对话体验，指代消解生效。

步骤：
1. 新增 `query_rewrite_node`（置于 intent_router 之前）：输入短期窗口 + 当前问题，输出独立完整问句；无指代时原样透传（prompt 判断）。
2. 改造 [memory.py](file:///e:/sjc/work/2026.08.31中期检查/其他/agent-dian001/MokioAgent-master/src/mokioclaw/graph/memory.py)：`working_memory` 层换成"短期会话窗口 + 当前意图 + 待确认槽位"，`history_summary_store` 换成"长期用户摘要"（历史主题、在办工单），检索证据作为新一层注入。
3. 长期摘要在会话结束时由压缩机制生成并写 SQLite `user_profile` 表，下次会话开始时装载。
4. 统一各节点的四层提示词组装入口（系统提示词 → 记忆 → 证据 → 问题）。

**验收**：构造多轮剧本"查 5G 套餐 → 那这个多少钱 → 帮我办这个"，第二轮指代被正确重写，第三轮正确路由到办理 Agent 且槽位自动带出前文套餐名；跨会话重开后能记起历史咨询主题。

### 阶段 6：自动化评测流水线

**目标**：评测闭环，一键出报告。

步骤：
1. 扩展 [trace.py](file:///e:/sjc/work/2026.08.31中期检查/其他/agent-dian001/MokioAgent-master/src/mokioclaw/core/trace.py)：补充记录意图判定、RAG 召回 Top-K、工具入参出参、反思校验结论。
2. 新增 `eval/` 目录：`collector.py`（轨迹采集）、`normalizer.py`（归一化为标准样本）、`rule_checks.py`（分流正确性/参数合法性/兜底触发/追问轮次断言）、`llm_judge.py`（质量与合规打分）、`report.py`（Markdown 报告）。
3. 准备 30 条标注评测集（覆盖四类业务 + 模糊 + 无关），`/eval/run` 一键执行并输出报告到 `eval/reports/`。

**验收**：评测集跑完后自动生成报告，包含通过率、LLM-Judge 分项得分、失败用例归因；报告中每条失败用例可回链到原始轨迹。

### 阶段 7：收尾与演示打磨

- 更新 README（新架构图、四类业务演示剧本、启动命令）。
- `uv run pytest -q` 全绿（为意图分流、RRF 融合、父子回溯、热插拔、评测规则各补单测）。
- 录制演示路径：`uvicorn` 起服务 → CLI 演示四类业务 → 热插拔一个 Skill → 跑一次评测出报告。

---

## 7. 改造后目标目录结构

```text
MokioAgent/
├─ src/mokioclaw/
│  ├─ api/            # 新增：FastAPI app、路由、依赖注入
│  ├─ graph/          # 改造：state/nodes/workflow/memory（客服对话主图）
│  ├─ rag/            # 新增：parsing/ingest 离线入库 + state/nodes/workflow RAG 检索子图
│  ├─ skills/         # 新增：Skill 基类、六个业务 Skill、动态注册中心
│  ├─ eval/           # 新增：评测流水线
│  ├─ db/             # 新增：SQLite 连接（aiosqlite）与 ORM 模型、schema.sql
│  ├─ core/           # 保留：trace、approval、checkpoint、session（改 SQLite）
│  ├─ prompts/        # 改造：意图识别/重写/思考/反思/兜底 prompt
│  ├─ providers/      # 保留：LLM 创建
│  └─ cli/            # 改造：HTTP/SSE 客户端
├─ knowledge/         # 新增：电信知识库原始文档
├─ eval/reports/      # 新增：评测报告输出
└─ tests/             # 扩充：意图/检索/工具/评测用例
```

## 8. 总验收标准

1. 四类业务（余额、套餐、故障、办理）多轮对话端到端可走通，意图分流正确。
2. 模糊意图追问 ≤5 轮后兜底；无关请求直接兜底。
3. RAG 答案附来源片段，父子分片回溯生效；Agent 链路有完整的"思考→调用→反思"轨迹。
4. Skill 热插拔：运行中增删工具目录，不重启即生效。
5. 指代消解：多轮指代问题被正确重写并路由。
6. 评测流水线一键运行，自动输出含规则校验与 LLM-Judge 结果的报告。
7. `uv run pytest -q` 全绿。

## 9. 风险与注意事项

| 风险 | 应对 |
| --- | --- |
| 本地加载 BGE-M3 + reranker 内存占用高 | 默认 CPU 推理 + 懒加载；内存紧张时 reranker 可降级为 Top-K 截断 |
| 阶段 1 先包 HTTP 会出现"同步 graph 卡事件循环" | 过渡期统一 `asyncio.to_thread`，阶段 4 彻底异步化后再移除 |
| 删除 file/bash 工具后旧测试大量失效 | 阶段 0 同步清理测试，避免红测试遗留到后续阶段 |
| 意图分流误判导致体验差 | prompt 中写清业务边界示例；评测阶段用规则校验回归兜底 |
| SQLite 并发写受限 | 本项目为演示场景读多写少，aiosqlite 单文件足够；开启 WAL 模式并把会话写入串行化（短事务）；若未来上生产可平滑换 MySQL（SQLAlchemy ORM 已隔离方言） |
