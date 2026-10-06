<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { userStore, userInitial } from '../stores/user.js'

const emit = defineEmits(['request-login', 'request-register'])

const open = ref(false)
const rootRef = ref(null)

const current = computed(() => userStore.current)
const users = computed(() => userStore.users)

function toggle() {
  open.value = !open.value
}

/**
 * 点击某个已注册账号：不再直接切换身份，而是带出该账号进入登录校验
 * （账号口令正确后才切换用户与会话空间）。
 */
function requestLogin(user) {
  open.value = false
  emit('request-login', user)
}

function requestRegister() {
  open.value = false
  emit('request-register')
}

function onClickOutside(event) {
  if (rootRef.value && !rootRef.value.contains(event.target)) open.value = false
}

onMounted(() => document.addEventListener('click', onClickOutside))
onUnmounted(() => document.removeEventListener('click', onClickOutside))
</script>

<template>
  <div ref="rootRef" class="user-switcher">
    <button class="switcher-btn" :class="{ open }" @click="toggle">
      <span class="avatar">{{ userInitial(current) }}</span>
      <span class="who">
        <span class="who-name">{{ current?.owner_name || '未登录' }}</span>
        <span class="who-phone">{{ current?.phone || '—' }}</span>
      </span>
      <span class="caret">▾</span>
    </button>

    <div v-if="open" class="dropdown">
      <div class="dropdown-header">
        <span>切换账号</span>
        <span class="dropdown-count">{{ users.length }} 个已注册</span>
      </div>
      <div class="dropdown-list">
        <button
          v-for="user in users"
          :key="user.phone"
          class="dropdown-item"
          :class="{ active: user.phone === current?.phone }"
          @click="requestLogin(user)"
        >
          <span class="avatar small">{{ userInitial(user) }}</span>
          <span class="item-text">
            <span class="item-name">{{ user.owner_name || user.username || '未知账号' }}</span>
            <span class="item-phone">
              <template v-if="user.username && user.username !== user.owner_name">
                {{ user.username }} ·
              </template>
              {{ user.phone }}
            </span>
          </span>
          <span v-if="user.phone === current?.phone" class="check">✓ 当前</span>
          <span v-else class="login-hint">登录</span>
        </button>
        <div v-if="!users.length" class="dropdown-empty">
          {{ userStore.loading ? '加载中...' : '暂无已注册账号' }}
        </div>
      </div>
      <div v-if="userStore.error" class="dropdown-error">{{ userStore.error }}</div>
      <div class="dropdown-actions">
        <button class="action-btn" @click="requestLogin(null)">登录其他账号</button>
        <button class="action-btn primary" @click="requestRegister">注册新账号</button>
      </div>
      <div class="dropdown-footer">切换账号需校验密码；各账号会话与记忆互相隔离</div>
    </div>
  </div>
</template>

<style scoped>
.user-switcher {
  position: relative;
}

.switcher-btn {
  display: flex;
  align-items: center;
  gap: 8px;
  background: transparent;
  border: 1px solid var(--border-color);
  border-radius: var(--radius);
  padding: 5px 10px 5px 6px;
  cursor: pointer;
  color: var(--text-primary);
}

.switcher-btn:hover,
.switcher-btn.open {
  border-color: var(--accent-dim);
  background: var(--bg-tertiary);
}

.avatar {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 26px;
  height: 26px;
  flex-shrink: 0;
  border-radius: 50%;
  background: var(--accent-dim);
  color: var(--accent);
  font-size: 13px;
  font-weight: 700;
}

.avatar.small {
  width: 22px;
  height: 22px;
  font-size: 11px;
}

.who {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  line-height: 1.25;
}

.who-name {
  font-size: 13px;
  font-weight: 600;
}

.who-phone {
  font-size: 11px;
  color: var(--text-muted);
  font-family: "SF Mono", Consolas, Monaco, monospace;
}

.caret {
  color: var(--text-muted);
  font-size: 11px;
}

.dropdown {
  position: absolute;
  top: calc(100% + 6px);
  right: 0;
  width: 288px;
  /* 限制整体高度并让列表成为唯一的滚动区，避免末项被底部操作区遮挡 */
  max-height: calc(100vh - 140px);
  display: flex;
  flex-direction: column;
  background: var(--bg-secondary);
  border: 1px solid var(--border-color);
  border-radius: var(--radius);
  box-shadow: var(--shadow);
  z-index: 50;
  overflow: hidden;
}

.dropdown-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-shrink: 0;
  padding: 10px 12px;
  border-bottom: 1px solid var(--border-color);
  color: var(--warning);
  font-size: 12px;
  font-weight: 700;
}

.dropdown-count {
  color: var(--text-muted);
  font-weight: 400;
}

.dropdown-list {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 6px;
}

.dropdown-item {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  padding: 8px;
  background: transparent;
  border: none;
  border-radius: var(--radius);
  cursor: pointer;
  text-align: left;
}

.dropdown-item:hover {
  background: var(--bg-tertiary);
}

.dropdown-item.active {
  background: var(--bg-tertiary);
}

.item-text {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-width: 0;
  line-height: 1.3;
}

.item-name {
  color: var(--text-primary);
  font-size: 13px;
}

.dropdown-item.active .item-name {
  color: var(--accent);
  font-weight: 600;
}

.item-phone {
  color: var(--text-muted);
  font-size: 11px;
  font-family: "SF Mono", Consolas, Monaco, monospace;
}

.check {
  color: var(--accent);
  font-size: 11px;
  flex-shrink: 0;
}

.login-hint {
  color: var(--text-subtle);
  font-size: 11px;
  flex-shrink: 0;
}

.dropdown-item:hover .login-hint {
  color: var(--accent);
}

.dropdown-empty,
.dropdown-error {
  padding: 14px 12px;
  text-align: center;
  color: var(--text-muted);
  font-size: 12px;
}

.dropdown-error {
  color: var(--error);
}

.dropdown-actions {
  display: flex;
  gap: 8px;
  flex-shrink: 0;
  padding: 8px 10px;
  border-top: 1px solid var(--border-color);
}

.action-btn {
  flex: 1;
  padding: 6px 0;
  background: transparent;
  border: 1px solid var(--border-color);
  border-radius: var(--radius);
  color: var(--text-secondary);
  font-size: 12px;
  cursor: pointer;
}

.action-btn:hover {
  background: var(--bg-tertiary);
  color: var(--text-primary);
}

.action-btn.primary {
  border-color: var(--accent-dim);
  color: var(--accent);
}

.dropdown-footer {
  flex-shrink: 0;
  padding: 8px 12px;
  border-top: 1px solid var(--border-color);
  background: var(--bg-secondary);
  color: var(--text-subtle);
  font-size: 11px;
}
</style>
