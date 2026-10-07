<script setup>
import { ref, watch, nextTick } from 'vue'
import { adminLogin } from '../api/admin.js'

const props = defineProps({
  visible: { type: Boolean, default: false },
})

const emit = defineEmits(['close', 'success'])

const password = ref('')
const loading = ref(false)
const error = ref('')
const inputRef = ref(null)

watch(
  () => props.visible,
  (visible) => {
    if (!visible) return
    password.value = ''
    error.value = ''
    loading.value = false
    nextTick(() => inputRef.value?.focus())
  },
  { immediate: true }
)

async function submit() {
  error.value = ''
  if (!password.value) {
    error.value = '请输入管理员密码。'
    return
  }
  loading.value = true
  try {
    const data = await adminLogin(password.value)
    emit('success', data)
  } catch (err) {
    error.value = err.message
  } finally {
    loading.value = false
  }
}

function close() {
  if (loading.value) return
  emit('close')
}
</script>

<template>
  <div v-if="visible" class="modal-overlay" @click.self="close">
    <div class="admin-modal">
      <div class="modal-header">
        <div class="header-text">
          <h3>管理员验证</h3>
          <span class="header-sub">验证通过后进入后台管理界面</span>
        </div>
        <button class="close-btn" @click="close">×</button>
      </div>

      <form class="modal-body" @submit.prevent="submit">
        <label class="field">
          <span class="field-label">管理员密码</span>
          <input
            ref="inputRef"
            v-model="password"
            class="input"
            type="password"
            autocomplete="current-password"
            placeholder="请输入管理员密码"
          />
        </label>
        <div v-if="error" class="error">{{ error }}</div>
      </form>

      <div class="modal-footer">
        <span class="footer-hint">演示管理员密码 123456</span>
        <button class="submit-btn" :disabled="loading" @click="submit">
          {{ loading ? '验证中...' : '进入管理界面' }}
        </button>
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
  z-index: 140;
}

.admin-modal {
  width: 400px;
  max-width: 92vw;
  display: flex;
  flex-direction: column;
  background: var(--bg-secondary);
  border: 1px solid var(--border-color);
  border-radius: var(--radius);
  box-shadow: var(--shadow);
  overflow: hidden;
}

.modal-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  padding: 16px;
  border-bottom: 1px solid var(--border-color);
}

.header-text {
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.modal-header h3 {
  margin: 0;
  color: var(--warning);
  font-size: 16px;
}

.header-sub {
  color: var(--text-subtle);
  font-size: 11px;
}

.close-btn {
  background: transparent;
  border: none;
  color: var(--text-muted);
  font-size: 20px;
  line-height: 1;
  cursor: pointer;
}

.modal-body {
  display: flex;
  flex-direction: column;
  gap: 14px;
  padding: 16px;
}

.field {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.field-label {
  color: var(--text-muted);
  font-size: 12px;
}

.input {
  width: 100%;
  padding: 9px 10px;
  background: var(--bg-tertiary);
  border: 1px solid var(--border-color);
  border-radius: var(--radius);
  color: var(--text-primary);
  font-size: 13px;
  font-family: inherit;
}

.input:focus {
  outline: none;
  border-color: var(--accent-dim);
}

.error {
  padding: 8px 10px;
  background: rgba(239, 111, 108, 0.12);
  border: 1px solid var(--error);
  border-radius: var(--radius);
  color: var(--error);
  font-size: 12px;
}

.modal-footer {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  padding: 12px 16px;
  border-top: 1px solid var(--border-color);
}

.footer-hint {
  color: var(--text-subtle);
  font-size: 11px;
}

.submit-btn {
  padding: 8px 16px;
  background: var(--accent-dim);
  border: 1px solid var(--accent-dim);
  border-radius: var(--radius);
  color: var(--text-primary);
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
}

.submit-btn:hover:not(:disabled) {
  background: var(--accent);
  border-color: var(--accent);
  color: var(--bg-primary);
}

.submit-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
</style>
