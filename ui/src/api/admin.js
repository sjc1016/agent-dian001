const API_BASE = import.meta.env.VITE_API_BASE || ''

async function request(path, options = {}) {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: { 'Content-Type': 'application/json', ...options.headers },
    ...options,
  })
  const data = await response.json().catch(() => ({}))
  if (!response.ok) {
    throw new Error(data.detail || `HTTP ${response.status}`)
  }
  return data
}

export function adminLogin(password) {
  return request('/api/v1/admin/login', {
    method: 'POST',
    body: JSON.stringify({ password }),
  })
}

export function listTables() {
  return request('/api/v1/admin/db/tables')
}

export function getTableSchema(table) {
  return request(`/api/v1/admin/db/tables/${encodeURIComponent(table)}/schema`)
}

export function listRows(table, { limit = 50, offset = 0, q = '' } = {}) {
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) })
  if (q) params.set('q', q)
  return request(`/api/v1/admin/db/tables/${encodeURIComponent(table)}/rows?${params}`)
}

export function createRow(table, data) {
  return request(`/api/v1/admin/db/tables/${encodeURIComponent(table)}/rows`, {
    method: 'POST',
    body: JSON.stringify({ data }),
  })
}

export function updateRow(table, pk, data) {
  return request(`/api/v1/admin/db/tables/${encodeURIComponent(table)}/rows/${encodeURIComponent(pk)}`, {
    method: 'PUT',
    body: JSON.stringify({ data }),
  })
}

export function deleteRow(table, pk) {
  return request(`/api/v1/admin/db/tables/${encodeURIComponent(table)}/rows/${encodeURIComponent(pk)}`, {
    method: 'DELETE',
  })
}

export function getRagStats() {
  return request('/api/v1/admin/rag/stats')
}

export function getRagSources() {
  return request('/api/v1/admin/rag/sources')
}

export function getRagSourceDetail(source) {
  return request(`/api/v1/admin/rag/sources/${encodeURIComponent(source)}/chunks`)
}

export function ingestRag({ path = '', all = false } = {}) {
  return request('/api/v1/admin/rag/ingest', {
    method: 'POST',
    body: JSON.stringify({ path, all }),
  })
}

export function uploadRag(file, source = '') {
  const form = new FormData()
  form.append('file', file)
  if (source) form.append('source', source)
  return request('/api/v1/admin/rag/upload', {
    method: 'POST',
    headers: {},
    body: form,
  })
}

export function deleteRagSource(source) {
  return request(`/api/v1/admin/rag/sources/${encodeURIComponent(source)}`, {
    method: 'DELETE',
  })
}

/* ---------------- FAQ 沉淀库 ---------------- */

export function getFaqStats() {
  return request('/api/v1/faq/stats')
}

export function listFaq({ status = '', category = '', q = '', limit = 50, offset = 0 } = {}) {
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) })
  if (status) params.set('status', status)
  if (category) params.set('category', category)
  if (q) params.set('q', q)
  return request(`/api/v1/faq?${params}`)
}

export function getFaqEntry(faqId) {
  return request(`/api/v1/faq/${encodeURIComponent(faqId)}`)
}

export function updateFaqStatus(faqId, status) {
  return request(`/api/v1/faq/${encodeURIComponent(faqId)}/status`, {
    method: 'PUT',
    body: JSON.stringify({ status }),
  })
}

export function reflowFaq() {
  return request('/api/v1/faq/reflow', { method: 'POST' })
}
