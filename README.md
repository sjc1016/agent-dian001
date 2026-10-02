<p align="center">
  <img src="./logo.png" alt="CongClaw Logo" width="460" />
</p>

<h1 align="center">电信客服智能体（基于 CongClaw 改造）</h1>

<p align="center">
  FastAPI + LangGraph 双引擎客服 Agent：RAG 快速检索保响应速度，Agent 深度推理保复杂业务处理能力。
</p>

## 项目简介

本项目在教学向 Mini CodeAgent **CongClaw** 的骨架上（LangGraph 编排、意图路由、分层记忆、工具注册、反思校验、链路观测），按"保留骨架、替换血肉"的思路改造为**电信客服业务智能体**，面向四类高频场景的多轮对话：

| 业务场景 | 示例 | 承接引擎 |
| --- | --- | --- |
| 基础业务咨询 | "5G 畅享套餐包含多少流量？" | RAG 快速检索子图 |
| 余额/账单查询 | "我还有多少钱话费？" | Agent 深度推理子图 |
| 故障报修 | "我家宽带从昨天开始断网" | Agent 子图（建单 + 查状态多工具并行） |
| 业务办理 | "帮我把套餐升级成 199 档" | Agent 子图（人工确认后执行） |

对话管控：意图模糊最多 **5 轮**追问澄清，超轮或无关请求（"写首诗"）自动兜底；支持指代消解（"那这个多少钱？"→ 完整问句重写）、跨会话长期用户摘要。

## 系统架构

```text
┌────────────────────────────────────────────────────────────────┐
│ 接入层                                                           │
│   CLI / TUI 演示客户端 ──HTTP/SSE──> FastAPI 服务                 │
│   /chat  /skills  /knowledge/ingest  /eval/run  /health         │
├────────────────────────────────────────────────────────────────┤
│ 编排层（LangGraph 主图，全链路原生 async）                         │
│                                                                 │
│   query_rewrite（指代消解）→ intent_router（五路意图分流）           │
│        │                                                        │
│        ├─ rag_query   → rag_subgraph（RAG 快查，见下图）            │
│        ├─ agent_service → agent_subgraph（深度推理）                │
│        │     think → act（多工具 asyncio.gather 并行）→ reflect     │
│        ├─ clarify（追问澄清，clarify_count ≤ 5）                    │
│        └─ fallback（兜底话术 + 服务边界引导）                        │
├────────────────────────────────────────────────────────────────┤
│ 能力层                                                           │
│  RAG 检索子图                       │  Skill 工具集（热插拔）        │
│   START ─┬─ retrieve_bm25 ─┐      │   query_balance    余额查询   │
│          └─ retrieve_dense ┘并行   │   query_package    套餐余量   │
│          ↓ rrf_fusion（RRF 融合）  │   list_packages    套餐列表   │
│          ↓ rerank（Cross-Encoder） │   report_fault     故障建单   │
│          ↓ evidence_gate（条件边） │   query_fault_status 工单进度 │
│          ├ 命中 → parent_lookup   │   change_package   套餐变更   │
│          │    （父分片回溯+邻域扩展）│    （写操作，需人工确认）      │
│          │    → generate（附来源） │   目录监听热加载，增删即生效    │
│          ├ 空/低置信 → rewrite_once│                              │
│          └ 仍无 → rag_fallback    │                              │
├────────────────────────────────────────────────────────────────┤
│ 记忆层                                                           │
│   短期：会话滑动窗口 + 当前意图 + 待确认槽位（SQLite session 表）     │
│   长期：跨会话用户摘要（历史咨询主题/在办工单/偏好套餐，user_profile）│
│   四层提示词组装：系统提示词 → 记忆 → 检索证据 → 用户问题            │
├────────────────────────────────────────────────────────────────┤
│ 数据层（本地轻量，零外部服务依赖）                                    │
│   Milvus Lite（child 粒度向量）   SQLite + WAL（元信息/会话/工单）   │
│   BGE-M3（本地 embedding）        bge-reranker（本地精排）           │
├────────────────────────────────────────────────────────────────┤
│ 评测层                                                           │
│   trace 采集 → 归一化 → 规则校验（分流/参数/兜底/追问轮次）           │
│   → LLM-Judge（质量/逻辑/合规 1~5 分）→ eval/reports/*.md          │
└────────────────────────────────────────────────────────────────┘
```

双引擎分工：意图识别把"知识型问题"分流到 RAG 子图（保速度），把"业务办理型请求"分流到 Agent 子图（保能力）。两个子图都是独立编译的 LangGraph `StateGraph`，主图只与它们做"问句进、答案出"的交互，每个检索/推理步骤天然进入 trace，直接作为评测流水线的轨迹数据。

## 快速开始

### 1. 环境准备

```bash
uv sync
```

下载本地模型（BGE-M3 + bge-reranker 到 `models/`）：

```bash
uv run python scripts/download_models.py
uv run python scripts/smoke_models.py   # 冒烟：输出 1 条向量 + 1 个 rerank 分数
```

### 2. 配置 `.env`

```text
API_KEY=...            # ChatOpenAI 兼容配置
MODEL=...
BASE_URL=...

DB_PATH=data/telecom_cs.db
MILVUS_DB_PATH=data/milvus_lite.db      # 中文路径下 faiss 索引异常时指向纯英文路径
EMBED_MODEL_PATH=models/bge-m3
RERANK_MODEL_PATH=models/bge-reranker
KNOWLEDGE_DIR=knowledge

# RAG 检索参数（可选，均有默认值）
RAG_RECALL_TOP_K=10
RAG_RERANK_INPUT_TOP_N=8                # CPU 上精排成本与候选数线性，8 时 P95≈2.4s
RAG_RERANK_TOP_K=5
RAG_RERANK_GATE_PROB=0.53               # 证据门阈值（按实测分布校准）
RAG_RERANK_GATE_RAW=0.15
RAG_PREWARM=1                           # 服务启动后台预热模型，消除首请求冷启动

# Skill 热插拔
SKILLS_EXTRA_DIR=data/skills            # 外部 Skill 目录
SKILL_WATCH=1                           # 目录监听开关
SKILL_WATCH_INTERVAL=2
```

完整示例见 [.env.example](./.env.example)。

### 3. 初始化知识库

```bash
uv run python -m congclaw.db           # 幂等建库 data/telecom_cs.db
uv run python -m congclaw.rag          # 入库 knowledge/ 全部文档（8 份，80 child 分片）
uv run python -m congclaw.rag --stats  # 查看知识库规模
```

### 4. 启动服务

```bash
uv run uvicorn congclaw.api.main:app --host 127.0.0.1 --port 8000
```

启动时自动：建库（幂等）→ Skill 注册中心扫描 + 目录监听 → 后台预热 BGE 模型。健康检查：

```bash
curl http://127.0.0.1:8000/health
```

### 5. 运行测试

```bash
uv run pytest -q        # 225 条全绿
```

## 四类业务演示剧本

服务启动后，CLI 作为 HTTP/SSE 客户端接入（默认 `http://127.0.0.1:8000`）。演示号码为种子用户 **13800138000**（张伟，5G畅享129元档）。

### 剧本 1：余额查询（Agent 单工具）

```bash
uv run congclaw "帮我查一下话费余额"
```

链路：意图分流 `agent_service` → think 决定调 `query_balance` → 返回余额 86.50 元 → reflect 校验通过。

### 剧本 2：套餐咨询（RAG 检索）

```bash
uv run congclaw "5G畅享套餐包含多少流量？"
```

链路：意图分流 `rag_query` → BM25 ∥ 稠密并行召回 → RRF 融合 → Cross-Encoder 精排 → 父分片回溯 → 生成答案并附来源片段编号。

### 剧本 3：故障报修（Agent 多工具并行）

```bash
uv run congclaw "我家宽带从昨天开始断网，帮我报修"
```

链路：`report_fault` 建单 + `query_fault_status` 查进度（`asyncio.gather` 并行）→ 返回工单号。

### 剧本 4：套餐变更（含人工确认）

```bash
uv run congclaw tui
# 第 1 轮：5G畅享199元档套餐怎么样
# 第 2 轮：那这个多少钱        ← 指代消解重写为完整问句
# 第 3 轮：帮我办这个          ← 路由到办理 Agent，槽位自动带出套餐名，推送确认卡片
# 第 4 轮：确认办理            ← 确认后才真正执行变更
```

`change_package` 是写操作：执行前先落 `pending_approval` 并向用户推确认卡片，用户确认后才执行；输入"取消"则作废。审批模式支持 `inline`（默认询问）/ `auto`（自动批准，演示用）/ `deny`（一律拒绝）：

```bash
uv run congclaw --approval-mode inline tui
```

### 对话管控演示

- 模糊追问："我想换个更划算的" → 追问澄清，最多 5 轮，第 5 轮仍不清自动兜底。
- 无关请求："帮我写首诗" → 直接兜底，礼貌说明服务边界并引导回四类业务。

## Skill 热插拔演示

服务运行中向外部 Skill 目录（默认 `data/skills/`）新增一个 Skill 文件，无需重启即被注册中心发现并可在下一轮对话调用：

```bash
# 方式一：API 热注册
curl -X POST http://127.0.0.1:8000/api/v1/skills -H "Content-Type: application/json" -d @new_skill.json

# 方式二：直接往 data/skills/ 目录写 .py 文件，watcher 线程按 SKILL_WATCH_INTERVAL 轮询发现
curl http://127.0.0.1:8000/api/v1/skills          # 查看当前 Skill 列表
curl -X DELETE http://127.0.0.1:8000/api/v1/skills/{name}   # 热卸载
```

## 自动化评测

一键跑 30 条标注评测集（四类业务 + 模糊 + 无关），自动输出含规则校验 + LLM-Judge 的 Markdown 报告：

```bash
curl -X POST http://127.0.0.1:8000/api/v1/eval/run
curl http://127.0.0.1:8000/api/v1/eval/reports          # 报告列表
curl http://127.0.0.1:8000/api/v1/eval/reports/{id}     # 报告详情
```

报告输出到 `eval/reports/`，包含通过率、LLM-Judge 分项得分（答案质量/推理逻辑/话术合规 1~5 分）、失败用例归因，每条失败用例可回链原始 trace 轨迹（`.congclaw/traces/{trace_id}/`）。

## 服务接口一览

| 接口 | 方法 | 说明 |
| --- | --- | --- |
| `/api/v1/chat` | POST（SSE） | 多轮对话主入口，流式返回 |
| `/api/v1/skills` | GET/POST/DELETE | Skill 列表、热注册、热卸载 |
| `/api/v1/knowledge/ingest` | POST | 知识库文档解析入库（另含 `/ingest/upload`、`/stats`） |
| `/api/v1/eval/run` | POST | 触发评测流水线 |
| `/api/v1/eval/reports/{id}` | GET | 获取评测报告 |
| `/health` | GET | 健康检查 |

## 目录结构

```text
MokioAgent/
├─ src/congclaw/
│  ├─ api/            # FastAPI app、路由（chat/skills/knowledge/eval）、依赖注入
│  ├─ graph/          # 客服对话主图：state/nodes/workflow/memory/profile_store
│  ├─ rag/            # RAG 子图：parsing/ingest 离线入库 + state/nodes/workflow 在线检索
│  │                  #   retrieval（BM25/稠密/精排）store（SQLite 父子分片）vectorstore（Milvus）
│  ├─ agent/          # Agent 推理子图：think / act（并行工具编排）/ reflect
│  ├─ skills/         # Skill 基类、registry 动态注册中心、catalog 六个业务 Skill
│  ├─ eval/           # 评测流水线：collector/normalizer/rule_checks/llm_judge/report/runner
│  ├─ db/             # SQLite 异步引擎（aiosqlite + WAL）、schema.sql、ORM 模型
│  ├─ core/           # trace、approval、checkpoint、session（SQLite 持久化）
│  ├─ prompts/        # 意图识别/查询重写/RAG/Agent 思考与反思 prompt
│  ├─ providers/      # ChatOpenAI 兼容 LLM 创建
│  └─ cli/            # HTTP/SSE 演示客户端（Rich CLI + Textual TUI）
├─ knowledge/         # 电信知识库原始文档（套餐/资费/宽带 FAQ，md/html/txt）
├─ models/            # BGE-M3、bge-reranker 本地模型
├─ data/              # telecom_cs.db、milvus_lite.db、外部 skills 目录
├─ eval/reports/      # 评测报告输出
├─ scripts/           # 模型下载/冒烟、Milvus Lite 验证
└─ tests/             # 225 条用例：意图分流/RAG 子图/Skill 热插拔/记忆/评测/端到端
```

## 关键设计说明

- **LangGraph 子图化**：RAG 与 Agent 均为独立编译的 `StateGraph` 子图，主图条件边挂载。检索全流程（并行召回→融合→精排→证据门→回溯→生成）图节点化，每步可独立观测、条件回退用条件边表达而非业务 if-else。
- **并行召回**：`START` 双出边 fan-out 到 `retrieve_bm25` / `retrieve_dense`，由 LangGraph 调度器并发执行，等价于 `asyncio.gather` 但天然进入 trace。
- **父子分片**：Child（1~3 句）保障召回精度，命中后回溯 Parent 段落块 + 前后邻域 child 扩展 + 去重合并，保障生成上下文完整无断句。
- **证据门校准**：reranker 概率/原始分双阈值（prob≥0.53 且 raw≥0.15，按实测分布校准），库外问题走 `rewrite_once`（限 1 次）→ `rag_fallback`，避免死循环。
- **热插拔注册中心**：`dict[路径→注销时 mtime]` 语义——内置 Skill 注销后文件内容变更自动恢复；外部 Skill 增删改经 watcher 轮询即生效。
- **性能**：本地 CPU 热身后单轮检索中位 2.06s / P95 2.41s（`RAG_RERANK_INPUT_TOP_N=8`）；首次冷启动约 32s 已由服务启动后台预热消除。调 `RAG_RERANK_INPUT_TOP_N=6` 可压到约 1.7s（弱相关问召回略有损失），或上 GPU/ONNX 量化进一步压缩。

## 演示彩排路径

```bash
# 1. 起服务
uv run uvicorn congclaw.api.main:app

# 2. CLI 演示四类业务（另开终端）
uv run congclaw "帮我查一下话费余额"
uv run congclaw "5G畅享套餐包含多少流量？"
uv run congclaw "我家宽带断网了，帮我报修"
uv run congclaw tui        # 多轮：套餐咨询 → 指代 → 办理确认

# 3. 热插拔 Skill（服务不重启）
curl -X POST http://127.0.0.1:8000/api/v1/skills -H "Content-Type: application/json" -d @demo_skill.json

# 4. 跑评测出报告
curl -X POST http://127.0.0.1:8000/api/v1/eval/run
```
