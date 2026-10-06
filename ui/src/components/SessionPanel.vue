<script setup>
import { ref, watch } from 'vue'
import { listSessions } from '../api/sessions.js'

const props = defineProps({
  visible: { type: Boolean, default: false },
})

const emit = defineEmits(['select', 'close', 'new'])

const sessions = ref([])
const loading = ref(false)
const error = ref('')

async function load() {
  loading.value = true
  error.value = ''
  try {
    const data = await listSessions()
    sessions.value = data.sessions || []
  } catch (err) {
    error.value = err.message
  } finally {
    loading.value = false
  }
}

watch(() => props.visible, (visible) => {
  if (visible) load()
})

function select(session) {
  emit('select', session)
}

function createNew() {
  emit('new')
}

function close() {
  emit('close')
}

function formatDate(value) {
  if (!value) return ''
  const date = new Date(value)
  return date.toLocaleString('zh-CN')
}
</script>

<template>
  <div v-if="visible" class="modal-overlay" @click.self="close">
    <div class="modal">
      <div class="modal-header">
        <h3>Sessions</h3>
        <button class="close-btn" @click="close">×</button>
      </div>
      <div class="modal-actions">
        <button class="primary-btn" @click="createNew">+ New Session</button>
        <button class="ghost-btn" @click="load" :disabled="loading">Refresh</button>
      </div>
      <div v-if="error" class="error">{{ error }}</div>
      <div v-if="loading" class="loading">Loading sessions...</div>
      <div v-else class="session-list">
        <div
          v-for="session in sessions"
          :key="session.session_id"
          class="session-item"
          @click="select(session)"
        >
          <div class="session-top">
            <span class="session-id">{{ session.session_id?.slice(0, 16) || 'unknown' }}</span>
            <span class="session-date">{{ formatDate(session.updated_at) }}</span>
          </div>
          <div class="session-route">route: {{ session.last_route || '(none)' }}</div>
          <div class="session-task">{{ session.last_task || 'No task' }}</div>
        </div>
        <div v-if="!sessions.length && !loading" class="empty">No sessions found.</div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.modal-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.7);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 100;
}

.modal {
  width: 560px;
  max-width: 90vw;
  max-height: 80vh;
  background: var(--bg-secondary);
  border: 1px solid var(--border-color);
  border-radius: var(--radius);
  box-shadow: var(--shadow);
  display: flex;
  flex-direction: column;
}

.modal-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 16px;
  border-bottom: 1px solid var(--border-color);
}

.modal-header h3 {
  margin: 0;
  color: var(--text-primary);
}

.close-btn {
  background: transparent;
  border: none;
  color: var(--text-muted);
  font-size: 20px;
  cursor: pointer;
}

.modal-actions {
  display: flex;
  gap: 8px;
  padding: 12px 16px;
}

.primary-btn {
  background: var(--accent);
  color: var(--bg-primary);
  border: none;
  padding: 6px 12px;
  border-radius: var(--radius);
  cursor: pointer;
  font-weight: 600;
}

.ghost-btn {
  background: transparent;
  color: var(--text-secondary);
  border: 1px solid var(--border-color);
  padding: 6px 12px;
  border-radius: var(--radius);
  cursor: pointer;
}

.session-list {
  overflow-y: auto;
  padding: 0 16px 16px;
}

.session-item {
  padding: 12px;
  border: 1px solid var(--border-color);
  border-radius: var(--radius);
  margin-bottom: 8px;
  cursor: pointer;
  transition: background 0.15s;
}

.session-item:hover {
  background: var(--bg-tertiary);
}

.session-top {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 4px;
}

.session-id {
  color: var(--accent);
  font-weight: 600;
  font-family: monospace;
}

.session-date {
  color: var(--text-subtle);
  font-size: 12px;
  flex-shrink: 0;
}

.session-route {
  color: var(--warning);
  font-size: 12px;
  margin-bottom: 4px;
}

.session-task {
  color: var(--text-secondary);
  font-size: 13px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.empty, .loading, .error {
  padding: 16px;
  text-align: center;
  color: var(--text-muted);
}

.error {
  color: var(--error);
}
</style>
