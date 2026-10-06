const API_BASE = import.meta.env.VITE_API_BASE || ''

/**
 * 拉取注册中心已加载的 Skill 元信息。
 *
 * Skill 是「能力定义」层：思考节点 bind_tools 时取的就是同一注册表，
 * 每个 Skill 经 to_openai_tool() 转成 LLM 可见的工具。
 * 因此这里的 total 代表当前可用的业务能力数量，与运行时的 tool_call 统计互补。
 */
export async function listSkills() {
  const response = await fetch(`${API_BASE}/api/v1/skills`)
  if (!response.ok) throw new Error(`HTTP ${response.status}`)
  return response.json()
}
