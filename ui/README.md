# Cong Agent Web UI

将原本的 TUI（Textual）终端界面替换为 Vue 3 网页前端，保留一致的深色主题、事件流展示、会话管理和实时 SSE 对话能力。

## 项目结构

```
ui/
├── index.html                  # 入口 HTML
├── package.json                # 依赖与脚本
├── vite.config.js              # Vite 配置（含 API 代理）
├── README.md                   # 本文件
└── src/
    ├── main.js                 # Vue 应用挂载
    ├── App.vue                 # 根布局与状态编排
    ├── style.css               # 全局样式与设计 token
    ├── api/
    │   ├── chat.js             # SSE 对话接口
    │   ├── sessions.js         # 历史会话接口（支持按用户前缀过滤）
    │   └── users.js            # 用户（客户）列表接口
    ├── stores/
    │   ├── session.js          # 全局会话状态
    │   └── user.js             # 当前用户状态与按用户的工作区命名
    ├── utils/
    │   └── events.js           # 事件摘要/分类/折叠逻辑（复刻 TUI）
    └── components/
        ├── ChatView.vue        # 聊天主区域
        ├── TurnBlock.vue       # 单轮对话（提问 + 思考过程折叠区 + 最终回复）
        ├── EventCard.vue       # 单条事件卡片
        ├── Sidebar.vue         # 右侧会话状态栏
        ├── SessionPanel.vue    # 会话列表面板
        ├── UserSwitcher.vue    # 用户切换下拉面板
        └── ApprovalDialog.vue  # 审批弹窗
```

## 多用户与会话隔离

用户即电信客户（以手机号码标识）。顶栏右侧的用户切换器可切换当前客户，切换后界面进入该客户独立的空间：

- 业务数据：号码决定 `account` / `user_package` / `fault_ticket` 的查询结果；
- 长期记忆：`user_profile` 按号码维度保存跨会话摘要；
- 会话隔离：每位客户的会话存放在 `.congclaw/workspaces/user-<号码>/` 下，每个会话一个子目录，
  因此同一客户可以有多个会话，而不同客户之间互不可见。

切换用户时前端会把 `phone` 随 `/api/v1/chat` 下发，并携带该用户的工作区路径；
会话面板则通过 `workspace_prefix` + `phone` 参数只拉取当前用户的会话。

### 默认用户与历史会话

默认演示用户为 **张伟（`13800138000`）**，即 `business_store.DEFAULT_DEMO_PHONE`：号码留空时后端
统一按该号码处理业务查询与长期记忆。

多用户改造之前创建的会话直接落在 `<项目根>/.congclaw/workspaces/workspace-<时间戳>-<随机>/`
（更早一代为 `.mokioclaw/`），路径中不含 `user-<号码>`，无法反推归属。这些会话统一划归默认用户：
默认用户在会话面板中除本人会话外，还会看到这批历史会话，并带「历史」标记。

实现上由 `core/paths.legacy_workspace_prefixes()` 给出遗留目录名前缀，`GET /api/v1/sessions`
在 `phone` 等于默认号码时把这些前缀作为 `extra_prefixes` 一并参与匹配（OR 语义）；
每条结果附带 `legacy: true/false`，供前端区分展示。改造前的会话未被移动或改写，
`SESSION_SUMMARY.md`、检查点等目录内容保持原样。

## 本地开发

1. 安装依赖

```bash
cd ui
npm install
```

2. 启动开发服务器

```bash
npm run dev
```

默认访问 http://localhost:5173。Vite 会把 `/api/*` 代理到后端的 `http://127.0.0.1:8000`。

3. 确保后端已启动

```bash
# 在项目根目录
python -m congclaw.api
# 或
uvicorn congclaw.api.main:app --host 127.0.0.1 --port 8000
```

## 后端适配说明

当前前端已通过 SSE 消费 `/api/v1/chat`，并可通过 `/api/v1/sessions` 读取历史会话。要完整支持网页端的 **inline 审批**，需要后端新增一个审批回调接口，例如：

```
POST /api/v1/approve
{
  "approval_id": "...",
  "approved": true
}
```

前端在 `App.vue` 中已预留 `handleApproval` 与 `onApprovalResolved`，接入该接口后即可在网页上完成审批确认。

## 快捷键

- `Enter`：发送消息
- `/new`：新建会话
- `/sessions`：打开会话面板
- `/clear` 或 `/cls`：清空事件区
- `Ctrl + S`：打开会话面板
- `Ctrl + L`：清空事件区
