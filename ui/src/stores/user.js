import { reactive } from 'vue'
import { listUsers } from '../api/users.js'

const STORAGE_KEY = 'cong.currentUserPhone'

// 兜底用户：后端未启动时保证界面仍可交互
const FALLBACK_USER = { phone: '13800138000', owner_name: '张伟' }

export const userStore = reactive({
  users: [],
  current: null,
  loading: false,
  error: '',
})

/**
 * 用户身份的会话隔离策略：每位用户拥有独立的工作区目录，
 * 其下每个会话一个子目录，因此既能按前缀过滤出「该用户的会话」，
 * 又允许同一用户拥有多个会话。
 */
export function userWorkspacePrefix(user) {
  return `.congclaw/workspaces/user-${user?.phone || 'unknown'}`
}

function compactStamp(date) {
  const pad = (value) => String(value).padStart(2, '0')
  return (
    `${date.getFullYear()}${pad(date.getMonth() + 1)}${pad(date.getDate())}` +
    `-${pad(date.getHours())}${pad(date.getMinutes())}${pad(date.getSeconds())}`
  )
}

/** 为指定用户生成一个新的会话工作区（相对项目根目录）。 */
export function newSessionWorkspace(user) {
  const rand = Math.random().toString(36).slice(2, 8)
  return `${userWorkspacePrefix(user)}/session-${compactStamp(new Date())}-${rand}`
}

export function userLabel(user) {
  if (!user) return '未选择用户'
  return user.owner_name ? `${user.owner_name} · ${user.phone}` : user.phone
}

/** 账号在登录框中的展示名：优先用户名，其次机主姓名。 */
export function userAccountName(user) {
  if (!user) return ''
  return user.username || user.owner_name || user.phone || ''
}

export function userInitial(user) {
  const name = user?.owner_name || ''
  return name ? name.slice(0, 1) : (user?.phone || '?').slice(-2)
}

export function rememberUserPhone(phone) {
  try {
    if (phone) localStorage.setItem(STORAGE_KEY, phone)
  } catch {
    /* 隐私模式下 localStorage 不可用，忽略 */
  }
}

export function loadRememberedPhone() {
  try {
    return localStorage.getItem(STORAGE_KEY) || ''
  } catch {
    return ''
  }
}

/**
 * 重新拉取账号列表（注册新账号后调用，保证下拉列表同步）。
 * 返回账号数组；失败时保留原列表并记录错误。
 */
export async function refreshUsers() {
  try {
    const data = await listUsers()
    userStore.users = data.users || []
    userStore.error = ''
  } catch (err) {
    userStore.error = err.message
  }
  return userStore.users
}

/** 把刚登录/刚注册的账号并入候选列表（已存在则原地更新）。 */
export function upsertUser(user) {
  if (!user?.phone) return
  const index = userStore.users.findIndex((item) => item.phone === user.phone)
  if (index >= 0) {
    userStore.users[index] = { ...userStore.users[index], ...user }
  } else {
    userStore.users.push({ ...user })
  }
}

/**
 * 加载用户列表并选出当前用户（优先上次选择，其次后端默认号码）。
 * 返回选中的用户，供调用方同步会话工作区。
 */
export async function loadAndResolveUser() {
  userStore.loading = true
  userStore.error = ''
  try {
    const data = await listUsers()
    userStore.users = data.users || []
    const remembered = loadRememberedPhone()
    const fallbackPhone = data.default_phone || FALLBACK_USER.phone
    const target =
      userStore.users.find((item) => item.phone === remembered) ||
      userStore.users.find((item) => item.phone === fallbackPhone) ||
      userStore.users[0] ||
      FALLBACK_USER
    return target
  } catch (err) {
    userStore.error = err.message
    userStore.users = [FALLBACK_USER]
    const remembered = loadRememberedPhone()
    return userStore.users.find((item) => item.phone === remembered) || FALLBACK_USER
  } finally {
    userStore.loading = false
  }
}
