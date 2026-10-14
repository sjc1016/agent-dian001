const API_BASE = import.meta.env.VITE_API_BASE || ''

/**
 * 拉取注册中心已加载的原子工具元信息。
 *
 * 原子工具是 LLM 唯一可直接调用的能力：思考节点把 openai_tools 交给
 * model.bind_tools 后由模型自主选择调用，因此这里的 total 代表当前模型可见的
 * 工具数量，与运行时的 tool_call 统计互补。
 */
export async function listTools() {
  const response = await fetch(`${API_BASE}/api/v1/tools`)
  if (!response.ok) throw new Error(`HTTP ${response.status}`)
  return response.json()
}
