const API_BASE = import.meta.env.VITE_API_BASE || ''

export async function listSessions(limit = 100) {
  const response = await fetch(`${API_BASE}/api/v1/sessions?limit=${limit}`)
  if (!response.ok) throw new Error(`HTTP ${response.status}`)
  return response.json()
}

export async function getSession(sessionId) {
  const response = await fetch(`${API_BASE}/api/v1/sessions/${sessionId}`)
  if (!response.ok) throw new Error(`HTTP ${response.status}`)
  return response.json()
}
