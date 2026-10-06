<script setup>
import { computed } from 'vue'
import { sessionStore } from '../stores/session.js'

const status = computed(() => (sessionStore.running ? 'running' : 'ready'))
const workspace = computed(() => sessionStore.workspace || '(waiting)')
const checkpoint = computed(() => sessionStore.checkpoint || '(waiting)')
const trace = computed(() => sessionStore.trace || '(waiting)')
const tools = computed(() => `${sessionStore.toolCount} total / ${sessionStore.failedToolCount} failed`)
const approvals = computed(() => String(sessionStore.approvalCount))
const todos = computed(() => sessionStore.currentTodoText())

const rows = computed(() => [
  {
    label: 'user',
    value: sessionStore.phone
      ? `${sessionStore.userName || '未知客户'} · ${sessionStore.phone}`
      : '(未选择)',
  },
  { label: 'status', value: status.value },
  { label: 'turns', value: String(sessionStore.runCount) },
  { label: 'session', value: sessionStore.sessionId ? sessionStore.sessionId.slice(0, 24) : '(starting)' },
  { label: 'route', value: sessionStore.route || '(none)' },
  { label: 'workspace', value: workspace.value },
  { label: 'checkpoint', value: checkpoint.value },
  { label: 'trace', value: trace.value },
  { label: 'tools', value: tools.value },
  { label: 'approvals', value: approvals.value },
  { label: 'todos', value: todos.value },
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
  min-width: 72px;
  flex-shrink: 0;
}

.sidebar-value {
  color: var(--text-secondary);
  word-break: break-word;
  white-space: pre-wrap;
}
</style>
