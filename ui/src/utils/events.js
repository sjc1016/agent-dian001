const DETAIL_TEXT_LIMIT = 20000

// 阶段 7「常见问答沉淀」事件的中文标签。
// 门控原因码与后端 congclaw/faq/gate.py 的返回值一一对应，改动需同步。
const FAQ_GATE_REASONS = {
  sediment_disabled: '沉淀能力已关闭',
  route_not_sedimentable: '该分支无可沉淀结论',
  empty_answer: '本轮无答复',
  fallback_turn: '兜底回复回合',
  pending_confirmation: '等待人工确认',
  no_evidence: '无检索证据',
  no_tool_result: '无成功的技能结果',
  question_too_short: '问句过短',
  privacy_risk: '含个人隐私信息',
}

const FAQ_ACTIONS = { create: '新建', merge: '合并', skip: '跳过' }

const FAQ_ENTRY_STATUSES = { draft: '草稿待审', approved: '已通过', published: '已发布', archived: '已归档' }

export function shorten(value, limit = 260) {
  const text = typeof value === 'string' ? value : JSON.stringify(value, null, 2)
  if (text.length <= limit) return text
  return text.slice(0, limit - 3) + '...'
}

export function summarizeEvent(event) {
  const eventType = event.type
  if (eventType === 'workspace') {
    return { title: 'Workspace', body: String(event.path || ''), category: 'workspace', style: 'blue' }
  }
  if (eventType === 'custom_event') {
    const payload = event.event
    if (payload && typeof payload === 'object') return summarizeCustomEvent(payload)
    return { title: 'Custom Event', body: shorten(payload), category: 'event', style: 'white' }
  }
  if (eventType === 'graph_event') {
    const payload = event.event
    if (payload && typeof payload === 'object') return summarizeGraphEvent(payload)
    return { title: 'Graph Event', body: shorten(payload), category: 'event', style: 'white' }
  }
  return { title: 'Event', body: shorten(event), category: 'event', style: 'white' }
}

function summarizeCustomEvent(event) {
  const eventType = event.type || 'event'
  if (eventType === 'intent_decision') {
    const route = String(event.route || 'workflow')
    return {
      title: 'Intent Router',
      body: `route: ${route}\nconfidence: ${event.confidence || 0}\nreason: ${shorten(event.reason || '', 600)}`,
      category: 'intent',
      style: route === 'chat' ? 'cyan' : 'magenta',
    }
  }
  if (eventType === 'chat_response') {
    return {
      title: 'CongClaw',
      body: `${shorten(event.response || '', 2400)}\nmode: ${event.mode || 'lightweight'} | reason: ${event.reason || ''}`,
      category: 'chat',
      style: 'cyan',
    }
  }
  if (eventType === 'session_started') {
    return {
      title: 'Session Started',
      body: `session: ${event.session_id || ''}\nworkspace: ${event.workspace || ''}\nturns: ${event.turn_index || 0}\nresumed: ${event.resumed || false}`,
      category: 'session',
      style: 'cyan',
    }
  }
  if (eventType === 'session_turn_started') {
    return {
      title: 'Session Turn',
      body: `turn: ${event.turn || 0}\n${shorten(event.task || '', 900)}`,
      category: 'session',
      style: 'magenta',
    }
  }
  if (eventType === 'session_turn_saved') {
    return {
      title: 'Session Saved',
      body: `turn: ${event.turn || 0}\nroute: ${event.route || ''}\nsummary: ${event.summary_file || ''}`,
      category: 'session',
      style: 'green',
    }
  }
  if (eventType === 'plan_snapshot' || eventType === 'todo_update') {
    return summarizePlan(event, eventType === 'plan_snapshot' ? 'Plan Snapshot' : 'Todo Updated')
  }
  if (eventType === 'tool_call') {
    const name = event.name || 'tool'
    const node = event.node || 'agent'
    return { title: `${node} · ${name}`, body: shorten(event.args || {}, 500), category: 'tool_call', style: 'magenta' }
  }
  if (eventType === 'tool_result') {
    const name = event.name || 'tool'
    const node = event.node || 'agent'
    const result = event.result
    const ok = result && typeof result === 'object' ? result.ok : null
    return { title: `${node} · ${name} result`, body: formatToolResult(result), category: 'tool_result', style: ok === false ? 'red' : 'green' }
  }
  // agent_loop 主路径的 Skill 调用事件（与遗留 planner 的 tool_call / tool_result 并存）。
  // 二者语义相同（都是能力被调用），因此复用同一 category，便于统计与配色统一。
  if (eventType === 'skill_call') {
    const name = event.name || 'skill'
    return { title: `技能调用 · ${name}`, body: shorten(event.args || {}, 500), category: 'tool_call', style: 'magenta' }
  }
  if (eventType === 'skill_result') {
    const name = event.name || 'skill'
    const ok = event.ok !== false
    return { title: `技能返回 · ${name}`, body: formatSkillResult(event), category: 'tool_result', style: ok ? 'green' : 'red' }
  }
  if (eventType === 'handoff') {
    return {
      title: `Handoff · ${event.from || 'agent'} -> ${event.to || 'agent'}`,
      body: shorten(event.task || '', 500),
      category: 'handoff',
      style: 'yellow',
    }
  }
  if (eventType === 'handoff_result') {
    return {
      title: `Handoff Result · ${event.from || 'agent'}`,
      body: shorten(event.summary || '', 600),
      category: 'handoff',
      style: 'green',
    }
  }
  if (eventType === 'search_results' || eventType === 'search_summary') {
    const sources = Array.isArray(event.sources) ? event.sources : []
    const answer = event.answer || event.summary || ''
    return { title: 'Search Summary', body: `${shorten(answer, 500)}\nsources: ${sources.length}`, category: 'search', style: 'cyan' }
  }
  if (eventType === 'memory_snapshot') {
    return { title: 'Memory Snapshot', body: formatMemorySnapshot(event), category: 'memory', style: 'cyan' }
  }
  if (eventType === 'context_monitor') {
    const shouldCompress = event.context_should_compress || event.should_compress || false
    return {
      title: 'Context Monitor',
      body: `tokens: ${event.context_token_count || event.token_count || 0} / ${event.context_token_limit || event.token_limit || 0}\ncompress: ${shouldCompress}\nnext: ${event.context_next_node || event.next_node || ''}`,
      category: 'context',
      style: shouldCompress ? 'yellow' : 'blue',
    }
  }
  if (eventType === 'context_compression') {
    const compression = latestCompression(event)
    return {
      title: 'Context Compression',
      body: `tokens: ${compression.before_tokens || ''} -> ${compression.after_tokens || ''}\nremoved messages: ${compression.removed_messages || ''}\nnext: ${compression.next_node || ''}\n${shorten(compression.summary || '', 500)}`,
      category: 'context',
      style: 'yellow',
    }
  }
  if (eventType === 'checkpoint_saved') {
    return {
      title: 'Checkpoint Saved',
      body: `mode: ${event.mode || ''}\nstatus: ${event.status || ''}\npath: ${event.path || ''}\nresume: ${event.resume_command || ''}`,
      category: 'checkpoint',
      style: event.status === 'interrupted' ? 'yellow' : 'blue',
    }
  }
  if (eventType === 'checkpoint_resumed') {
    return {
      title: 'Checkpoint Resumed',
      body: `mode: ${event.mode || ''}\nworkspace: ${event.workspace || ''}\nsource: ${event.source || ''}\nfallback: ${event.fallback || false}`,
      category: 'checkpoint',
      style: 'green',
    }
  }
  if (eventType === 'trace_summary') {
    const nodes = event.node_visits || {}
    const nodeText = Object.entries(nodes).map(([node, count]) => `${node}:${count}`).join(', ') || '(none)'
    return {
      title: 'Trace Summary',
      body: `trace: ${event.trace_id || ''}\nstatus: ${event.status || ''}\npath: ${event.trace_dir || ''}\nnodes: ${nodeText}\ntools: ${event.tool_calls || 0} total / ${event.failed_tool_calls || 0} failed`,
      category: 'trace',
      style: event.status === 'finished' ? 'green' : 'yellow',
    }
  }
  // ---- 阶段 7：常见问答沉淀 ----
  // body 首行统一为 `sediment: <token>`，供 eventCategory 判定卡片配色；
  // 由于折叠态只显示标题，标题必须自带结论，展开后才看明细。
  if (eventType === 'faq_sediment_start') {
    return {
      title: `沉淀判定 · ${event.route || ''}`,
      body: `sediment: info\nroute: ${event.route || ''}\nstage: 规则门控 → 判重召回 → 抽取 → 落库`,
      category: 'faq',
    }
  }
  if (eventType === 'faq_gate') {
    const passed = event.passed === true
    const reason = FAQ_GATE_REASONS[event.reason] || event.reason || ''
    return {
      title: passed ? '沉淀门控 · 通过' : `沉淀门控 · 跳过（${reason}）`,
      body: `sediment: info\npassed: ${passed}\nreason: ${passed ? '(通过)' : reason}\ncode: ${event.reason || ''}\nquestion: ${event.question || ''}`,
      category: 'faq',
    }
  }
  if (eventType === 'faq_match') {
    const count = Number(event.candidate_count || 0)
    const lines = [
      'sediment: info',
      `candidates: ${count}`,
      `top_similarity: ${event.top_similarity || 0}`,
    ]
    if (event.error) lines.push(`error: ${event.error}`)
    lines.push(`question: ${event.question || ''}`)
    return {
      title: count ? `沉淀判重 · ${count} 条相近条目` : '沉淀判重 · 无相近条目',
      body: lines.join('\n'),
      category: 'faq',
    }
  }
  if (eventType === 'faq_sediment_decision') {
    const action = String(event.action || 'skip')
    const label = FAQ_ACTIONS[action] || action
    const confidence = event.confidence === null || event.confidence === undefined ? '' : `\nconfidence: ${event.confidence}`
    return {
      title: `沉淀决策 · ${label}`,
      body: `sediment: info\naction: ${label} (${action})\nreason: ${event.reason || ''}${confidence}\ncanonical_question: ${event.canonical_question || ''}\ncategory: ${event.category || ''}`,
      category: 'faq',
    }
  }
  if (eventType === 'faq_sediment_saved') {
    const status = FAQ_ENTRY_STATUSES[event.status] || event.status || ''
    const action = FAQ_ACTIONS[event.action] || event.action || ''
    return {
      title: `沉淀入库 · ${event.faq_id || ''}（${status}）`,
      body: `sediment: saved\nfaq_id: ${event.faq_id || ''}\naction: ${action}\nentry_status: ${status}\ncategory: ${event.category || ''}\nmerge_count: ${event.merge_count || 0}\ncanonical_question: ${event.canonical_question || ''}`,
      category: 'faq',
    }
  }
  if (eventType === 'faq_sediment_skipped') {
    return {
      title: `沉淀跳过 · ${shorten(event.reason || '模型判定无需沉淀', 60)}`,
      body: `sediment: skipped\nreason: ${event.reason || ''}`,
      category: 'faq',
    }
  }
  if (eventType === 'faq_sediment_error') {
    return {
      title: `沉淀异常 · ${event.stage || 'unknown'} 阶段`,
      body: `sediment: error\nstage: ${event.stage || ''}\nerror: ${event.error || ''}`,
      category: 'faq',
    }
  }
  if (eventType === 'faq_sediment_trace') {
    const nodes = Array.isArray(event.node_sequence) ? event.node_sequence : []
    return {
      title: `沉淀链路 · ${nodes.join(' → ') || '(空)'}`,
      body: `sediment: info\nnode_sequence: ${nodes.join(' → ') || '(空)'}`,
      category: 'faq',
    }
  }
  if (eventType === 'faq_sediment_finished') {
    const action = String(event.action || 'skip')
    const persisted = event.persisted === true
    const token = action === 'error' ? 'error' : (persisted ? 'saved' : 'skipped')
    const suffix = persisted ? `入库 ${event.faq_id || ''}` : FAQ_ACTIONS[action] || action
    return {
      title: `沉淀完成 · ${suffix}`,
      body: `sediment: ${token}\naction: ${FAQ_ACTIONS[action] || action} (${action})\npersisted: ${persisted}\nfaq_id: ${event.faq_id || ''}`,
      category: 'faq',
    }
  }
  return { title: String(eventType), body: shorten(event, DETAIL_TEXT_LIMIT), category: 'event', style: 'white' }
}

function summarizeGraphEvent(payload) {
  if (!payload) return { title: 'Graph Event', body: '', category: 'graph', style: 'white' }
  const node = String(Object.keys(payload)[0])
  const update = payload[node]
  if (!update || typeof update !== 'object') return { title: node, body: shorten(update), category: 'graph', style: 'white' }
  if (node === 'planner') return summarizePlan(update, 'Planner')
  if (node === 'actor' || node === 'codeAgent') {
    const summary = update.code_agent_summary || update.last_actor_summary || update
    return { title: 'codeAgent Summary', body: shorten(summary, DETAIL_TEXT_LIMIT), category: 'agent', style: 'cyan' }
  }
  if (node === 'verifier') {
    return { title: 'Verifier', body: formatVerifier(update), category: 'verifier', style: update.passed ? 'green' : 'red' }
  }
  if (node === 'final') {
    return { title: 'Final', body: shorten(update.final_answer || update, 1200), category: 'final', style: 'green' }
  }
  if (node === 'context_monitor') return summarizeCustomEvent({ type: 'context_monitor', ...update })
  if (node === 'context_compressor') return summarizeCustomEvent({ type: 'context_compression', ...update })
  if (node === 'memory_snapshot') return summarizeCustomEvent({ type: 'memory_snapshot', ...update })
  return { title: node, body: shorten(update, DETAIL_TEXT_LIMIT), category: 'graph', style: 'white' }
}

function summarizePlan(update, title) {
  const todos = Array.isArray(update.todos) ? update.todos : []
  const counts = todoCounts(todos)
  const lines = []
  if (update.plan_summary) lines.push(shorten(update.plan_summary, 500))
  if (todos.length) {
    lines.push(`todos: ${counts}`)
    for (const todo of todos.slice(0, 6)) {
      lines.push(`- ${todo.status || 'pending'}: ${todo.content || todo.description || ''}`)
    }
    if (todos.length > 6) lines.push(`... ${todos.length - 6} more`)
  }
  const commands = Array.isArray(update.verification_commands) ? update.verification_commands : []
  if (commands.length) lines.push('verify: ' + commands.slice(0, 3).join('; '))
  return { title, body: lines.join('\n'), category: 'plan', style: 'cyan' }
}

function formatToolResult(result) {
  if (!result || typeof result !== 'object') return shorten(result, 700)
  const keys = ['ok', 'exit_code', 'timed_out', 'duration_ms', 'background', 'requires_approval', 'approved', 'approval_id', 'risk_reason', 'error', 'path', 'stdout_path', 'stderr_path']
  const lines = keys.filter((key) => key in result).map((key) => `${key}: ${result[key]}`)
  if (result.stdout) lines.push('stdout:\n' + shorten(result.stdout, 500))
  if (result.stderr) lines.push('stderr:\n' + shorten(result.stderr, 500))
  if (result.todos) lines.push(`todos: ${result.todos.length} item(s)`)
  return lines.join('\n') || shorten(result, 700)
}

/**
 * 格式化 Skill 返回事件。
 * 注意：skill_result 的成败标记在**顶层** `ok` 字段（见 congclaw/agent/nodes.py），
 * 与 tool_result 把结果包在 `result` 对象里不同，这里显式用 `ok: false` 便于统一判定失败。
 */
function formatSkillResult(event) {
  const lines = [`ok: ${event.ok !== false}`]
  if (event.call_id) lines.push(`call_id: ${event.call_id}`)
  if (event.error) lines.push(`error: ${event.error}`)
  if (event.preview) lines.push(`preview:\n${shorten(event.preview, 600)}`)
  return lines.join('\n')
}

function formatMemorySnapshot(event) {
  const layers = event.layers || {}
  return [
    `rules: ${shorten(layers.rules || '', 180)}`,
    `working_memory: ${shorten(layers.working_memory || '', 220)}`,
    `history_summary_store: ${shorten(layers.history_summary_store || '', 220)}`,
    `todos=${event.todo_count || 0} sources=${event.source_count || 0} handoffs=${event.handoff_count || 0}`,
  ].join('\n')
}

function formatVerifier(update) {
  const lines = []
  if (update.verifier_summary) lines.push(shorten(update.verifier_summary, 500))
  const checks = Array.isArray(update.verification_checks) ? update.verification_checks : []
  for (const check of checks.slice(0, 6)) {
    const status = check.passed ? 'PASS' : 'FAIL'
    lines.push(`${status}: ${check.name || 'check'} - ${shorten(check.detail || '', 160)}`)
  }
  lines.push(`passed=${update.passed} | attempts=${update.attempts}`)
  return lines.join('\n')
}

function latestCompression(event) {
  const events = event.compression_events
  if (Array.isArray(events) && events.length && typeof events[events.length - 1] === 'object') return events[events.length - 1]
  return event
}

function todoCounts(todos) {
  const counts = {}
  for (const todo of todos) {
    const status = String(todo.status || 'pending')
    counts[status] = (counts[status] || 0) + 1
  }
  return Object.entries(counts).sort(([a], [b]) => a.localeCompare(b)).map(([status, count]) => `${status}:${count}`).join(', ') || '0'
}

export function compactBody(summary) {
  const title = summary.title
  const body = summary.body || ''
  if (summary.category === 'session') {
    return firstMatchingLine(body, ['route:', 'turn:', 'workspace:', 'session:']) || shorten(body, 140)
  }
  if (summary.category === 'intent') {
    const route = lineValue(body, 'route')
    const reason = lineValue(body, 'reason')
    return `route ${route || 'workflow'}` + (reason ? ` · ${shorten(reason, 90)}` : '')
  }
  if (summary.category === 'chat') {
    return shorten(body.split('\nmode:')[0], 2400)
  }
  if (summary.category === 'plan') {
    const todos = lineValue(body, 'todos')
    const first = body.split('\n')[0] || title
    return shorten(first + (todos ? ` · todos ${todos}` : ''), 180)
  }
  if (summary.category === 'tool_call') return shorten(body, 160)
  if (summary.category === 'tool_result') {
    const ok = lineValue(body, 'ok')
    const path = lineValue(body, 'path') || lineValue(body, 'stdout_path')
    const pieces = [ok ? `ok=${ok}` : 'tool result']
    if (path) pieces.push(path)
    return shorten(pieces.join(' · '), 180)
  }
  if (summary.category === 'handoff') return shorten(body, 180)
  if (summary.category === 'memory') return 'memory snapshot updated'
  if (summary.category === 'context') {
    return firstMatchingLine(body, ['tokens:', 'compress:', 'next:']) || shorten(body, 160)
  }
  if (summary.category === 'checkpoint') {
    const status = lineValue(body, 'status') || lineValue(body, 'mode')
    return status ? `checkpoint ${status}` : 'checkpoint updated'
  }
  if (summary.category === 'trace') {
    const status = lineValue(body, 'status')
    const tools = lineValue(body, 'tools')
    return [status ? `status ${status}` : '', tools ? `tools ${tools}` : ''].filter(Boolean).join(' · ')
  }
  if (summary.category === 'final') {
    return shorten(body.split('\n')[0] || body, 220)
  }
  if (summary.category === 'verifier') {
    return shorten(body.split('\n')[0] || body, 180)
  }
  return shorten(body, 180)
}

export function shouldCollapse(summary) {
  return summary.category !== 'chat' && summary.category !== 'final' && summary.category !== 'verifier'
}

export function eventCategory(summary) {
  if (summary.category === 'final' || summary.category === 'trace') return 'success'
  if (summary.category === 'verifier' || summary.category === 'tool_result') {
    // formatToolResult / formatSkillResult 均以「ok: false」标记调用失败
    if (summary.body && (summary.body.includes('FAIL') || summary.body.includes('ok: false'))) return 'error'
  }
  if (summary.category === 'faq') {
    // 沉淀事件首行 `sediment: <token>` 由 summarizeCustomEvent 统一写入
    const token = lineValue(summary.body || '', 'sediment')
    if (token === 'error') return 'error'
    if (token === 'saved') return 'success'
    return 'info'
  }
  if (['plan', 'tool_call', 'handoff', 'context', 'checkpoint'].includes(summary.category)) return 'running'
  return 'info'
}

export function shouldHideEvent(event) {
  if (event.type === 'workspace') return true
  const payload = event.event
  if (event.type === 'graph_event' && payload && typeof payload === 'object') {
    const hiddenNodes = new Set(['intent_router'])
    return Object.keys(payload).every((node) => hiddenNodes.has(node))
  }
  if (event.type === 'custom_event' && payload && typeof payload === 'object') {
    return ['session_started', 'session_turn_started', 'memory_snapshot'].includes(payload.type)
  }
  return false
}

/**
 * 从事件流中提取本轮面向用户的最终回复。
 * 入口图四类终端节点（rag_answer / agent_loop / clarify / fallback）与
 * 遗留 complex 工作流的 final 节点都会在状态更新里携带 `final_answer`。
 */
export function extractFinalAnswer(event) {
  if (!event) return ''
  if (event.type === 'graph_event') {
    const payload = event.event
    if (!payload || typeof payload !== 'object') return ''
    let answer = ''
    for (const update of Object.values(payload)) {
      if (update && typeof update === 'object' && update.final_answer) {
        answer = String(update.final_answer)
      }
    }
    return answer
  }
  if (event.type === 'custom_event') {
    const payload = event.event
    if (!payload || typeof payload !== 'object') return ''
    if (payload.type === 'chat_response' && payload.response) return String(payload.response)
  }
  return ''
}

/** 从思考过程卡片中兜底推断最终回复（用于流结束时仍未捕获 final_answer 的场景）。 */
export function fallbackAnswerFromThinking(thinking) {
  if (!Array.isArray(thinking) || !thinking.length) return ''
  for (let i = thinking.length - 1; i >= 0; i -= 1) {
    const card = thinking[i]
    if (card && (card.category === 'final' || card.category === 'chat' || card.category === 'verifier')) {
      return card.detail || card.body || ''
    }
  }
  return ''
}

function lineValue(body, key) {
  const prefix = `${key}:`
  for (const line of body.split('\n')) {
    const stripped = line.trim()
    if (stripped.startsWith(prefix)) return stripped.slice(prefix.length).trim()
  }
  return ''
}

function firstMatchingLine(body, prefixes) {
  for (const line of body.split('\n')) {
    const stripped = line.trim()
    if (prefixes.some((p) => stripped.startsWith(p))) return stripped
  }
  return ''
}

export function categoryClass(category) {
  return {
    running: 'event-running',
    success: 'event-success',
    error: 'event-error',
    info: 'event-info',
    user: 'event-user',
  }[category] || 'event-info'
}

export function categoryColor(category) {
  return {
    running: 'var(--warning)',
    success: 'var(--success)',
    error: 'var(--error)',
    info: 'var(--accent)',
    user: 'var(--text-primary)',
  }[category] || 'var(--text-secondary)'
}
