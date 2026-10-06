# Mokio Agent Web UI

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
    │   └── sessions.js         # 历史会话接口
    ├── stores/
    │   └── session.js          # 全局会话状态
    ├── utils/
    │   └── events.js           # 事件摘要/分类/折叠逻辑（复刻 TUI）
    └── components/
        ├── ChatView.vue        # 聊天主区域
        ├── EventCard.vue       # 单条事件卡片
        ├── Sidebar.vue         # 右侧会话状态栏
        ├── SessionPanel.vue    # 会话列表面板
        └── ApprovalDialog.vue  # 审批弹窗
```

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
