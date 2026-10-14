<script setup>
import { computed } from 'vue'
import { sessionStore } from '../stores/session.js'

// 面板文案面向业务演示统一使用中文；括号内为对应的后端术语
const status = computed(() => (sessionStore.running ? '运行中' : '就绪'))
const workspace = computed(() => sessionStore.workspace || '(等待中)')
// 能力维度（skills）：注册中心已加载的编排技能数量（由 intent_router 确定性挑选）
const skills = computed(() => `${sessionStore.skillCount} 个`)
// 能力维度（tools）：注册中心已加载的原子工具数量（思考节点 bind_tools 交给模型自主选择）
const toolRegister = computed(() => `${sessionStore.toolRegistryCount} 个`)
// 调用维度（calls）：本轮原子工具的实际调用次数与失败数
const toolCalls = computed(() => `${sessionStore.toolCount} 次 / 失败 ${sessionStore.failedToolCount}`)
const approvals = computed(() => String(sessionStore.approvalCount))
const todos = computed(() => sessionStore.currentTodoText())

const rows = computed(() => [
  {
    label: '用户',
    value: sessionStore.phone
      ? `${sessionStore.userName || '未知客户'} · ${sessionStore.phone}`
      : '(未选择)',
  },
  { label: '状态', value: status.value },
  { label: '轮次', value: String(sessionStore.runCount) },
  { label: '会话', value: sessionStore.sessionId ? sessionStore.sessionId.slice(0, 24) : '(启动中)' },
  { label: '路由', value: sessionStore.route || '(无)' },
  { label: '工作区', value: workspace.value },
  { label: '已注册技能', value: skills.value },
  { label: '已注册工具', value: toolRegister.value },
  { label: '工具调用', value: toolCalls.value },
  { label: '待确认', value: approvals.value },
  { label: '待办', value: todos.value },
])
</script>

<template>
  <aside class="sidebar">
    <h3 class="sidebar-title">Session</h3>
    <div class="sidebar-rows">
      <div v-for="row in rows" :key="row.label" class="sidebar-row">
        <span class="sidebar-label">{{ row.label }}</span>
        <span class="sidebar-value">{{ row.value }}</span>
      </div>
    </div>
  </aside>
</template>

<style scoped>
.sidebar {
  width: 280px;
  min-width: 240px;
  height: 100%;
  padding: 16px;
  background: var(--bg-secondary);
  border-left: 1px solid var(--border-color);
  overflow-y: auto;
}

.sidebar-title {
  margin: 0 0 12px 0;
  color: var(--warning);
  font-size: 14px;
  font-weight: 700;
}

.sidebar-row {
  display: flex;
  gap: 12px;
  padding: 4px 0;
  font-size: 13px;
}

.sidebar-label {
  color: var(--accent);
  font-weight: 600;
  /* 容纳「已注册技能」等 5 字中文标签，并保证各行列左对齐 */
  min-width: 76px;
  flex-shrink: 0;
  white-space: nowrap;
}

.sidebar-value {
  color: var(--text-secondary);
  word-break: break-word;
  white-space: pre-wrap;
}
</style>
