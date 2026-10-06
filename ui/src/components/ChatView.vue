<script setup>
import { ref, nextTick, watch } from 'vue'
import EventCard from './EventCard.vue'
import TurnBlock from './TurnBlock.vue'

const props = defineProps({
  notices: { type: Array, default: () => [] },
  turns: { type: Array, default: () => [] },
  running: { type: Boolean, default: false },
})

const emit = defineEmits(['send', 'clear'])

const input = ref('')
const scrollRef = ref(null)

function send() {
  const text = input.value.trim()
  if (!text || props.running) return
  emit('send', text)
  input.value = ''
}

function onKeydown(e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    send()
  }
}

function scrollToBottom() {
  nextTick(() => {
    const el = scrollRef.value
    if (el) el.scrollTop = el.scrollHeight
  })
}

watch(() => props.turns.length, scrollToBottom)
watch(() => props.notices.length, scrollToBottom)

defineExpose({ scrollToBottom })
</script>

<template>
  <div class="chat-view">
    <div ref="scrollRef" class="events-area">
      <div v-if="notices.length" class="notices">
        <EventCard
          v-for="(notice, index) in notices"
          :key="`notice-${index}`"
          :title="notice.title"
          :body="notice.body"
          :detail="notice.detail"
          :category="notice.category"
          :collapsed="notice.collapsed"
        />
      </div>

      <TurnBlock
        v-for="turn in turns"
        :key="turn.id"
        :turn="turn"
      />

      <div v-if="!turns.length" class="empty-hint">
        输入你的问题，Agent 的最终回复会以对话形式展示在这里
      </div>
    </div>

    <div class="input-area">
      <div class="input-row">
        <span class="prompt">❯</span>
        <input
          v-model="input"
          type="text"
          class="task-input"
          placeholder="描述你的问题，回车发送"
          :disabled="running"
          @keydown="onKeydown"
        />
        <button class="send-btn" :disabled="running || !input.trim()" @click="send">发送</button>
      </div>
      <div class="input-hint">
        回车发送 · /new 新建会话 · /sessions 会话列表 · Ctrl+L 清空
      </div>
    </div>
  </div>
</template>

<style scoped>
.chat-view {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
  height: 100%;
}

.events-area {
  flex: 1;
  overflow-y: auto;
  padding: 16px 20px;
  background: var(--bg-primary);
}

.notices {
  margin-bottom: 20px;
}

.empty-hint {
  text-align: center;
  color: var(--text-subtle);
  padding: 48px 0;
  font-size: 13px;
}

.input-area {
  padding: 12px 20px;
  border-top: 1px solid var(--border-color);
  background: var(--bg-secondary);
}

.input-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 12px;
  border: 1px solid var(--accent-dim);
  border-radius: var(--radius);
  background: var(--bg-secondary);
}

.prompt {
  color: var(--accent);
  font-weight: bold;
  font-size: 16px;
}

.task-input {
  flex: 1;
  background: transparent;
  border: none;
  outline: none;
  color: var(--text-primary);
  font-size: 14px;
}

.task-input::placeholder {
  color: var(--text-subtle);
}

.send-btn {
  background: var(--accent);
  color: var(--bg-primary);
  border: none;
  padding: 6px 14px;
  border-radius: var(--radius);
  cursor: pointer;
  font-weight: 600;
}

.send-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.input-hint {
  margin-top: 6px;
  color: var(--text-subtle);
  font-size: 12px;
}
</style>
