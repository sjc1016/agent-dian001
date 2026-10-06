const API_BASE = import.meta.env.VITE_API_BASE || ''

/** 解析响应体：FastAPI 的错误统一在 detail 字段。 */
async function readBody(response) {
  const data = await response.json().catch(() => ({}))
  if (!response.ok) {
    const detail = typeof data?.detail === 'string' ? data.detail : `HTTP ${response.status}`
    throw new Error(detail)
  }
  return data
}

/**
 * 登录：account 支持手机号或用户名。
 * 成功后返回 { ok, user: { phone, username, owner_name } }。
 */
export async function login(account, password) {
  const response = await fetch(`${API_BASE}/api/v1/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ account, password }),
  })
  return readBody(response)
}

/**
 * 注册：后端会写入 account 表并建立 user_profile 记忆锚点。
 * @param {{phone: string, password: string, username?: string, owner_name?: string}} payload
 */
export async function register(payload) {
  const response = await fetch(`${API_BASE}/api/v1/auth/register`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  return readBody(response)
}

/** 按关键词检索已注册账号（手机号/用户名/机主姓名），用于登录框搜索提示。 */
export async function searchAccounts(keyword = '', limit = 8) {
  const params = new URLSearchParams({ limit: String(limit) })
  if (keyword) params.set('keyword', keyword)
  const response = await fetch(`${API_BASE}/api/v1/auth/accounts?${params.toString()}`)
  if (!response.ok) throw new Error(`HTTP ${response.status}`)
  return response.json()
}
