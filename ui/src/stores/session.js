import { reactive } from 'vue'

export const sessionStore = reactive({
  workspace: '',
  sessionId: '',
  // 当前用户（电信客户）身份：号码决定业务数据、长期记忆与会话隔离
  phone: '',
  userName: '',
  turn: 0,
  runCount: 0,
  route: '',
  checkpoint: '',
  trace: '',
  toolCount: 0,
  failedToolCount: 0,
  // 注册中心已加载的编排技能总数（能力维度，全局一致，不随会话切换变化）
  skillCount: 0,
  // 注册中心已加载的原子工具总数（同上，全局一致）
  toolRegistryCount: 0,
  approvalCount: 0,
  todos: [],
  running: false,

  reset() {
    // 注意：phone / userName 属于用户身份，skillCount / toolRegistryCount 属于全局能力清单，
    // 三者跨会话保持不变，仅在切换用户时由外部显式覆盖，因此不在此处清空。
    this.workspace = ''
    this.sessionId = ''
    this.turn = 0
    this.runCount = 0
    this.route = ''
    this.checkpoint = ''
    this.trace = ''
    this.toolCount = 0
    this.failedToolCount = 0
    this.approvalCount = 0
    this.todos = []
    this.running = false
  },

  setRunning(value) {
    this.running = value
  },

  updateFromEvent(event) {
    if (event.type === 'workspace') {
      this.workspace = event.path || this.workspace
      return
    }
    const payload = event.event
    if (!payload || typeof payload !== 'object') return

    if (Array.isArray(payload.todos)) {
      this.todos = payload.todos
    }

    // 调用统计：tool_call / tool_result 是唯一能力调用事件
    // - Agent 子图把失败标记放在顶层 ok；遗留 planner 包在 result.ok 里。
    if (payload.type === 'tool_call') {
      this.toolCount += 1
    }
    if (payload.type === 'tool_result') {
      const result = payload.result && typeof payload.result === 'object' ? payload.result : payload
      if (result.ok === false) this.failedToolCount += 1
      if (result.requires_approval) this.approvalCount += 1
    }
    if (payload.type === 'checkpoint_saved') {
      this.checkpoint = payload.path || ''
    }
    if (payload.type === 'trace_summary') {
      this.trace = payload.trace_dir || ''
    }
    if (payload.type === 'session_started') {
      this.sessionId = payload.session_id || ''
      this.turn = payload.turn_index || 0
      this.workspace = payload.workspace || this.workspace
    }
    if (payload.type === 'session_turn_started') {
      this.turn = payload.turn || this.turn
    }
    if (payload.type === 'session_turn_saved') {
      this.turn = payload.turn || this.turn
      this.route = payload.route || ''
    }
  },

  currentTodoText() {
    if (!this.todos.length) return '(暂无)'
    const counts = {}
    for (const todo of this.todos) {
      const status = todo.status || 'pending'
      counts[status] = (counts[status] || 0) + 1
    }
    const countText = Object.entries(counts)
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([key, value]) => `${key}:${value}`)
      .join(', ')
    const current = this.todos.find((t) => t.status === 'in_progress')
    if (current) {
      const text = current.content || current.description || ''
      return `${countText}\n${text.length > 120 ? text.slice(0, 120) + '...' : text}`
    }
    return countText
  },
})
