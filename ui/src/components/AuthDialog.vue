<script setup>
import { ref, computed, watch, nextTick } from 'vue'
import { login, register, searchAccounts } from '../api/auth.js'

const props = defineProps({
  visible: { type: Boolean, default: false },
  initialMode: { type: String, default: 'login' },
  /** 从用户下拉选中某账号进来时的预填值（手机号或用户名） */
  prefill: { type: String, default: '' },
})

const emit = defineEmits(['close', 'success'])

const mode = ref('login')
const account = ref('')
const password = ref('')
const phone = ref('')
const username = ref('')
const ownerName = ref('')
const password2 = ref('')

const suggestions = ref([])
const showSuggestions = ref(false)
const loading = ref(false)
const error = ref('')

const accountInput = ref(null)
let searchTimer = null

const title = computed(() => (mode.value === 'login' ? '登录账号' : '注册新账号'))
const canSubmitLogin = computed(() => account.value.trim() && password.value && !loading.value)
const canSubmitRegister = computed(
  () => phone.value.trim() && password.value && password2.value && !loading.value
)

/** 打开弹窗时重置表单；预填值来自用户下拉中被点击的账号。 */
watch(
  () => props.visible,
  (visible) => {
    if (!visible) {
      showSuggestions.value = false
      return
    }
    resetForm()
    mode.value = props.initialMode === 'register' ? 'register' : 'login'
    account.value = props.prefill || ''
    nextTick(() => {
      if (mode.value === 'login' && !account.value) accountInput.value?.focus()
      if (account.value) loadSuggestions()
    })
  },
  { immediate: true }
)

function resetForm() {
  account.value = ''
  password.value = ''
  phone.value = ''
  username.value = ''
  ownerName.value = ''
  password2.value = ''
  suggestions.value = []
  showSuggestions.value = false
  error.value = ''
  loading.value = false
}

function switchMode(next) {
  if (mode.value === next) return
  mode.value = next
  error.value = ''
  showSuggestions.value = false
}

/** 输入账号时按关键词检索已注册账号，给出列表提示（轻量防抖）。 */
function onAccountInput() {
  if (searchTimer) clearTimeout(searchTimer)
  searchTimer = setTimeout(loadSuggestions, 220)
}

async function loadSuggestions() {
  try {
    const data = await searchAccounts(account.value.trim(), 6)
    suggestions.value = data.accounts || []
    showSuggestions.value = true
  } catch {
    suggestions.value = []
  }
}

/** 选中提示项：直接填入账号，登录密码仍需用户输入。 */
function pickSuggestion(item) {
  account.value = item.username || item.phone
  showSuggestions.value = false
}

function hideSuggestions() {
  showSuggestions.value = false
}

async function submitLogin() {
  error.value = ''
  if (!account.value.trim()) {
    error.value = '请输入手机号或用户名。'
    return
  }
  if (!password.value) {
    error.value = '请输入登录密码。'
    return
  }
  loading.value = true
  try {
    const data = await login(account.value.trim(), password.value)
    emit('success', data.user)
  } catch (err) {
    error.value = err.message
  } finally {
    loading.value = false
  }
}

async function submitRegister() {
  error.value = ''
  if (!phone.value.trim()) {
    error.value = '请输入手机号。'
    return
  }
  if (!password.value) {
    error.value = '请设置登录密码。'
    return
  }
  if (password.value !== password2.value) {
    error.value = '两次输入的密码不一致，请重新确认。'
    return
  }
  loading.value = true
  try {
    const data = await register({
      phone: phone.value.trim(),
      username: username.value.trim(),
      password: password.value,
      owner_name: ownerName.value.trim(),
    })
    emit('success', data.user)
  } catch (err) {
    error.value = err.message
  } finally {
    loading.value = false
  }
}

function submit() {
  if (mode.value === 'login') submitLogin()
  else submitRegister()
}

function close() {
  if (loading.value) return
  emit('close')
}
</script>

<template>
  <div v-if="visible" class="modal-overlay" @click.self="close">
    <div class="auth-modal">
      <div class="modal-header">
        <div class="header-text">
          <h3>{{ title }}</h3>
          <span class="header-sub">不同账号拥有独立的会话与记忆空间</span>
        </div>
        <button class="close-btn" @click="close">×</button>
      </div>

      <div class="tabs">
        <button
          class="tab"
          :class="{ active: mode === 'login' }"
          @click="switchMode('login')"
        >登录</button>
        <button
          class="tab"
          :class="{ active: mode === 'register' }"
          @click="switchMode('register')"
        >注册</button>
      </div>

      <form class="modal-body" @submit.prevent="submit">
        <!-- 登录 -->
        <template v-if="mode === 'login'">
          <label class="field">
            <span class="field-label">账号</span>
            <input
              ref="accountInput"
              v-model="account"
              class="input"
              type="text"
              autocomplete="username"
              placeholder="手机号或用户名"
              @input="onAccountInput"
              @focus="loadSuggestions"
              @blur="hideSuggestions"
            />
          </label>
          <div v-if="showSuggestions && suggestions.length" class="suggest">
            <button
              v-for="item in suggestions"
              :key="item.phone"
              type="button"
              class="suggest-item"
              @mousedown.prevent="pickSuggestion(item)"
            >
              <span class="suggest-name">{{ item.owner_name || item.username || '未知账号' }}</span>
              <span class="suggest-meta">{{ item.username }} · {{ item.phone }}</span>
            </button>
          </div>

          <label class="field">
            <span class="field-label">密码</span>
            <input
              v-model="password"
              class="input"
              type="password"
              autocomplete="current-password"
              placeholder="请输入密码"
            />
          </label>
        </template>

        <!-- 注册 -->
        <template v-else>
          <label class="field">
            <span class="field-label">手机号 <i class="required">*</i></span>
            <input
              v-model="phone"
              class="input"
              type="text"
              autocomplete="tel"
              placeholder="11 位手机号，作为账号唯一标识"
            />
          </label>
          <div class="field-row">
            <label class="field">
              <span class="field-label">用户名</span>
              <input
                v-model="username"
                class="input"
                type="text"
                placeholder="可留空，默认取姓名"
              />
            </label>
            <label class="field">
              <span class="field-label">机主姓名</span>
              <input v-model="ownerName" class="input" type="text" placeholder="可留空" />
            </label>
          </div>
          <div class="field-row">
            <label class="field">
              <span class="field-label">密码 <i class="required">*</i></span>
              <input
                v-model="password"
                class="input"
                type="password"
                autocomplete="new-password"
                placeholder="至少 6 位"
              />
            </label>
            <label class="field">
              <span class="field-label">确认密码 <i class="required">*</i></span>
              <input
                v-model="password2"
                class="input"
                type="password"
                autocomplete="new-password"
                placeholder="再次输入密码"
              />
            </label>
          </div>
          <p class="hint">注册信息会同步写入账号数据库，随后可用手机号或用户名登录。</p>
        </template>

        <div v-if="error" class="error">{{ error }}</div>
      </form>

      <div class="modal-footer">
        <span class="footer-hint">演示账号默认密码 123456</span>
        <button class="submit-btn" :disabled="mode === 'login' ? !canSubmitLogin : !canSubmitRegister" @click="submit">
          {{ loading ? '处理中...' : mode === 'login' ? '登录' : '注册并登录' }}
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
  z-index: 120;
}

.auth-modal {
  width: 420px;
  max-width: 92vw;
  max-height: 90vh;
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
  color: var(--accent);
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

.tabs {
  display: flex;
  flex-shrink: 0;
  border-bottom: 1px solid var(--border-color);
}

.tab {
  flex: 1;
  padding: 10px 0;
  background: transparent;
  border: none;
  border-bottom: 2px solid transparent;
  color: var(--text-muted);
  font-size: 13px;
  cursor: pointer;
}

.tab:hover {
  color: var(--text-secondary);
}

.tab.active {
  color: var(--accent);
  border-bottom-color: var(--accent);
  font-weight: 600;
}

.modal-body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 16px 16px 4px;
}

.field {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-bottom: 12px;
}

.field-row {
  display: flex;
  gap: 10px;
}

.field-row .field {
  flex: 1;
  min-width: 0;
}

.field-label {
  color: var(--text-muted);
  font-size: 12px;
}

.required {
  color: var(--error);
  font-style: normal;
}

.input {
  width: 100%;
  padding: 8px 10px;
  background: var(--bg-primary);
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

.suggest {
  margin: -6px 0 12px;
  border: 1px solid var(--border-color);
  border-radius: var(--radius);
  background: var(--bg-tertiary);
  max-height: 176px;
  overflow-y: auto;
}

.suggest-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
  width: 100%;
  padding: 7px 10px;
  background: transparent;
  border: none;
  border-bottom: 1px solid var(--border-color);
  cursor: pointer;
  text-align: left;
}

.suggest-item:last-child {
  border-bottom: none;
}

.suggest-item:hover {
  background: var(--bg-secondary);
}

.suggest-name {
  color: var(--text-primary);
  font-size: 13px;
}

.suggest-meta {
  color: var(--text-muted);
  font-size: 11px;
  font-family: "SF Mono", Consolas, Monaco, monospace;
}

.hint {
  margin: 0 0 12px;
  color: var(--text-subtle);
  font-size: 11px;
  line-height: 1.5;
}

.error {
  margin-bottom: 12px;
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
  flex-shrink: 0;
  padding: 14px 16px;
  border-top: 1px solid var(--border-color);
}

.footer-hint {
  color: var(--text-subtle);
  font-size: 11px;
}

.submit-btn {
  background: var(--accent);
  color: var(--bg-primary);
  border: none;
  padding: 8px 18px;
  border-radius: var(--radius);
  font-weight: 600;
  font-size: 13px;
  cursor: pointer;
}

.submit-btn:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
</style>
