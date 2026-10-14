const API_BASE = import.meta.env.VITE_API_BASE || ''

/**
 * 拉取注册中心已加载的编排技能元信息。
 *
 * 编排技能是「多工具确定性编排」层：不进 LLM 的 bind_tools 工具表，
 * 由主图 intent_router 依据 intent_hint 挑选后按代码控制流执行。
 * 因此这里的 total 代表当前可用的编排能力数量。
 */
export async function listSkills() {
  const response = await fetch(`${API_BASE}/api/v1/skills`)
  if (!response.ok) throw new Error(`HTTP ${response.status}`)
  return response.json()
}
