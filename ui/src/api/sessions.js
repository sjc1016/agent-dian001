const API_BASE = import.meta.env.VITE_API_BASE || ''

/**
 * 列出历史会话。
 * @param {number} limit 返回条数上限
 * @param {string} [workspacePrefix] 用户工作区前缀（相对路径），
 *   传入后仅返回该用户的会话，实现「不同用户各自管理自己的会话」。
 * @param {string} [phone] 当前用户号码；默认演示用户会额外获得改造前的
 *   未归属历史会话（返回项带 legacy 标记）。
 */
export async function listSessions(limit = 100, workspacePrefix = '', phone = '') {
  const params = new URLSearchParams({ limit: String(limit) })
  if (workspacePrefix) params.set('workspace_prefix', workspacePrefix)
  if (phone) params.set('phone', phone)
  const response = await fetch(`${API_BASE}/api/v1/sessions?${params.toString()}`)
  if (!response.ok) throw new Error(`HTTP ${response.status}`)
  return response.json()
}

export async function getSession(sessionId) {
  const response = await fetch(`${API_BASE}/api/v1/sessions/${sessionId}`)
  if (!response.ok) throw new Error(`HTTP ${response.status}`)
  return response.json()
}
