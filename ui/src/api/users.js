const API_BASE = import.meta.env.VITE_API_BASE || ''

/** 拉取可切换的用户列表（后端按 account 账户表返回号码 + 机主姓名）。 */
export async function listUsers(limit = 50) {
  const response = await fetch(`${API_BASE}/api/v1/users?limit=${limit}`)
  if (!response.ok) throw new Error(`HTTP ${response.status}`)
  return response.json()
}
