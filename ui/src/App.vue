<script setup>
import { ref, reactive, computed, onMounted, onUnmounted } from 'vue'
import ChatView from './components/ChatView.vue'
import Sidebar from './components/Sidebar.vue'
import SessionPanel from './components/SessionPanel.vue'
import ApprovalDialog from './components/ApprovalDialog.vue'
import { sessionStore } from './stores/session.js'
import { streamChat } from './api/chat.js'
import { getSession } from './api/sessions.js'
import {
  summarizeEvent,
  shouldHideEvent,
  shouldCollapse,
  extractFinalAnswer,
  fallbackAnswerFromThinking,
} from './utils/events.js'

// notices：与具体提问无关的系统提示（欢迎语、会话切换、错误等），以可折叠卡片形式置顶展示
const notices = ref([])
// turns：一轮完整对话，含用户提问、思考过程（折叠卡片）、Agent 最终回复
const turns = ref([])
const running = ref(false)
const showSessions = ref(false)
const approvalRequest = ref(null)
const approvalResolver = ref(null)
const currentController = ref(null)
const chatViewRef = ref(null)

const isRunning = computed(() => running.value)
let turnSeq = 0

function pushNotice(title, body, category = 'info', collapsed = true, detail = null) {
  notices.value.push({ title, body, category, collapsed, detail: detail ?? body })
}

function pushWelcome() {
  pushNotice(
    'Mokio Agent',
    '电信客服智能体已就绪。可以直接提问，也可以用 /new 开启新会话。',
    'info',
    true,
    '每轮对话的中间执行过程会收拢在「思考过程」中，最终回复以对话气泡形式展示。'
  )
}

function createTurn(question) {
  turnSeq += 1
  // 必须用 reactive 包装：后续通过 onEvent 回调增量写入 answer/thinking/status，
  // 若直接持有原始对象引用，变更不会触发视图更新
  const turn = reactive({
    id: turnSeq,
    question,
    answer: '',
    thinking: [],
    status: 'running',
  })
  turns.value.push(turn)
  return turn
}

function pushThinking(turn, summary) {
  turn.thinking.push({
    title: summary.title,
    body: summary.body,
    category: summary.category,
    collapsed: shouldCollapse(summary),
    detail: summary.detail ?? summary.body,
  })
}

function clearAll() {
  turns.value = []
  notices.value = []
  pushWelcome()
}

function resetState() {
  sessionStore.reset()
  running.value = false
  approvalRequest.value = null
  approvalResolver.value = null
}

function handleCommand(text) {
  if (text === '/new') {
    resetState()
    turns.value = []
    notices.value = []
    pushNotice('New Session', '已开启新的会话工作区。', 'info', false)
    pushWelcome()
    return true
  }
  if (text === '/sessions') {
    showSessions.value = true
    return true
  }
  if (text === '/clear' || text === '/cls') {
    clearAll()
    return true
  }
  return false
}

async function loadSessionHistory(sessionId) {
  if (!sessionId) return
  try {
    const session = await getSession(sessionId)
    if (!session || !session.recent_turns?.length) return
    pushNotice(
      `历史记录 · ${session.recent_turns.length} 条`,
      '以下是该会话之前的对话：',
      'info',
      false
    )
    // 成对还原：user 提问 + 紧跟的 assistant 回复合成一轮对话
    let pending = null
    for (const item of session.recent_turns) {
      const role = item.role || ''
      const content = item.content || ''
      if (role === 'user') {
        if (pending) turns.value.push(pending)
        pending = reactive({ id: ++turnSeq, question: content, answer: '', thinking: [], status: 'done' })
      } else if (role === 'assistant') {
        if (!pending) {
          pending = reactive({ id: ++turnSeq, question: '', answer: content, thinking: [], status: 'done' })
        } else {
          pending.answer = content
        }
        turns.value.push(pending)
        pending = null
      }
    }
    if (pending) turns.value.push(pending)
  } catch (err) {
    pushNotice('历史记录加载失败', err.message, 'error', false)
  }
}

function handleApproval(event) {
  return new Promise((resolve) => {
    approvalRequest.value = event
    approvalResolver.value = resolve
  })
}

function onApprovalResolved({ approved }) {
  if (approvalResolver.value) {
    approvalResolver.value(approved)
    approvalResolver.value = null
    approvalRequest.value = null
  }
}

function startStream(task) {
  if (running.value) return
  running.value = true
  sessionStore.setRunning(true)
  sessionStore.runCount += 1

  const turn = createTurn(task)

  currentController.value = streamChat({
    message: task,
    workspace: sessionStore.workspace || undefined,
    sessionId: sessionStore.sessionId || undefined,
    approvalMode: 'inline',
    maxAttempts: 3,
    onEvent: (event) => {
      sessionStore.updateFromEvent(event)

      // 先尝试提取本轮最终回复，再决定是否作为思考过程展示
      const answer = extractFinalAnswer(event)
      if (answer) turn.answer = answer

      if (event.type === 'custom_event' && event.event?.type === 'approval_request') {
        handleApproval(event.event).then((approved) => {
          pushNotice('审批结果', approved ? '已批准' : '已拒绝', approved ? 'success' : 'error', false)
        })
        return
      }

      if (shouldHideEvent(event)) return
      pushThinking(turn, summarizeEvent(event))
    },
    onError: (err) => {
      pushThinking(turn, { title: 'Error', body: err.message, category: 'error', detail: err.message })
      finishRun(turn, 'failed')
    },
    onDone: () => {
      finishRun(turn, 'finished')
    },
  })
}

function finishRun(turn, status) {
  running.value = false
  sessionStore.setRunning(false)
  turn.status = status === 'finished' ? 'done' : 'failed'
  if (!turn.answer) {
    turn.answer = fallbackAnswerFromThinking(turn.thinking)
  }
}

function onSend(text) {
  if (handleCommand(text)) return
  startStream(text)
}

function onSelectSession(session) {
  showSessions.value = false
  resetState()
  turns.value = []
  notices.value = []
  sessionStore.workspace = session.workspace || ''
  sessionStore.sessionId = session.session_id || ''
  sessionStore.turn = session.turn_index || 0
  sessionStore.route = session.last_route || ''
  pushNotice('已切换会话', session.workspace, 'info', false)
  loadSessionHistory(session.session_id)
}

function onNewSession() {
  showSessions.value = false
  resetState()
  turns.value = []
  notices.value = []
  pushNotice('New Session', '已开启新的会话工作区。', 'info', false)
  pushWelcome()
}

function handleKeydown(e) {
  if (e.ctrlKey && e.key.toLowerCase() === 'l') {
    e.preventDefault()
    clearAll()
  }
  if (e.ctrlKey && e.key.toLowerCase() === 's') {
    e.preventDefault()
    showSessions.value = true
  }
}

onMounted(() => {
  pushWelcome()
  window.addEventListener('keydown', handleKeydown)
})

onUnmounted(() => {
  window.removeEventListener('keydown', handleKeydown)
  if (currentController.value) currentController.value.abort()
})
</script>

<template>
  <div class="app">
    <header class="top-bar">
      <div class="logo-block">
        <pre class="logo">
 ███╗   ███╗ ██████╗ ██╗  ██╗██╗ ██████╗
 ████╗ ████║██╔═══██╗██║ ██╔╝██║██╔═══██╗
 ██╔████╔██║██║   ██║█████╔╝ ██║██║   ██║
 ██║╚██╔╝██║██║   ██║██╔═██╗ ██║██║   ██║
 ██║ ╚═╝ ██║╚██████╔╝██║  ██╗██║╚██████╔╝
 ╚═╝     ╚═╝ ╚═════╝ ╚═╝  ╚═╝╚═╝ ╚═════╝
        </pre>
      </div>
      <div class="title-block">
        <h1>Mokio Agent</h1>
        <div class="status" :class="{ running: isRunning }">
          {{ isRunning ? 'running' : 'ready' }}
        </div>
        <div class="subtitle">电信客服智能体 Web UI</div>
      </div>
      <div class="actions">
        <button @click="showSessions = true">Sessions</button>
        <button @click="clearAll">Clear</button>
      </div>
    </header>

    <main class="main">
      <ChatView
        ref="chatViewRef"
        :notices="notices"
        :turns="turns"
        :running="isRunning"
        @send="onSend"
        @clear="clearAll"
      />
      <Sidebar />
    </main>

    <SessionPanel
      :visible="showSessions"
      @select="onSelectSession"
      @new="onNewSession"
      @close="showSessions = false"
    />

    <ApprovalDialog
      :request="approvalRequest"
      @resolve="onApprovalResolved"
    />
  </div>
</template>

<style scoped>
.app {
  display: flex;
  flex-direction: column;
  height: 100vh;
  background: var(--bg-primary);
}

.top-bar {
  display: flex;
  align-items: center;
  gap: 16px;
  height: 100px;
  min-height: 100px;
  padding: 0 20px;
  background: var(--bg-secondary);
  border-bottom: 1px solid var(--border-color);
}

.logo-block {
  flex-shrink: 0;
}

.logo {
  margin: 0;
  color: var(--accent);
  font-size: 10px;
  line-height: 1.2;
  font-family: "SF Mono", Consolas, Monaco, monospace;
}

.title-block {
  flex: 1;
  display: flex;
  flex-direction: column;
  justify-content: center;
  gap: 2px;
}

.title-block h1 {
  margin: 0;
  color: var(--text-primary);
  font-size: 20px;
  font-weight: 700;
}

.status {
  color: var(--text-muted);
  font-size: 13px;
}

.status.running {
  color: var(--warning);
}

.subtitle {
  color: var(--accent);
  font-size: 13px;
}

.actions {
  display: flex;
  gap: 8px;
}

.actions button {
  background: transparent;
  color: var(--text-secondary);
  border: 1px solid var(--border-color);
  padding: 6px 12px;
  border-radius: var(--radius);
  cursor: pointer;
  font-size: 13px;
}

.actions button:hover {
  background: var(--bg-tertiary);
}

.main {
  flex: 1;
  display: flex;
  overflow: hidden;
}
</style>
