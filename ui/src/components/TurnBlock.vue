<script setup>
import { ref, computed } from 'vue'
import EventCard from './EventCard.vue'

const props = defineProps({
  turn: { type: Object, required: true },
})

const thinkingOpen = ref(false)

const stepCount = computed(() => props.turn.thinking?.length || 0)
const hasThinking = computed(() => stepCount.value > 0)
const statusText = computed(() => {
  if (props.turn.status === 'failed') return '执行失败'
  if (props.turn.status === 'running') return '思考中...'
  return ''
})
</script>

<template>
  <div class="turn-block">
    <div class="message user-message">
      <div class="message-label">你</div>
      <div class="message-bubble user-bubble">{{ turn.question }}</div>
    </div>

    <div v-if="hasThinking" class="thinking-section">
      <button class="thinking-toggle" @click="thinkingOpen = !thinkingOpen">
        <span class="thinking-icon">{{ thinkingOpen ? '▼' : '▶' }}</span>
        <span>思考过程</span>
        <span class="thinking-count">{{ stepCount }} 步</span>
      </button>
      <div v-if="thinkingOpen" class="thinking-body">
        <EventCard
          v-for="(card, index) in turn.thinking"
          :key="index"
          :title="card.title"
          :body="card.body"
          :detail="card.detail"
          :category="card.category"
          :collapsed="card.collapsed"
        />
      </div>
    </div>

    <div class="message assistant-message">
      <div class="message-label">Mokio Agent</div>
      <div v-if="turn.answer" class="message-bubble assistant-bubble">{{ turn.answer }}</div>
      <div v-else-if="statusText" class="message-bubble assistant-bubble placeholder">{{ statusText }}</div>
      <div v-else class="message-bubble assistant-bubble placeholder">未获取到回复</div>
    </div>
  </div>
</template>

<style scoped>
.turn-block {
  margin-bottom: 28px;
}

.message {
  display: flex;
  flex-direction: column;
}

.user-message {
  align-items: flex-end;
}

.assistant-message {
  align-items: flex-start;
}

.message-label {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-muted);
  margin-bottom: 4px;
}

.message-bubble {
  max-width: 78%;
  padding: 12px 14px;
  border-radius: var(--radius);
  line-height: 1.7;
  white-space: pre-wrap;
  word-break: break-word;
  font-size: 14px;
}

.user-bubble {
  background: var(--bg-tertiary);
  color: var(--text-primary);
  border: 1px solid var(--border-color);
}

.assistant-bubble {
  background: var(--bg-secondary);
  color: var(--text-primary);
  border: 1px solid var(--accent-dim);
}

.placeholder {
  color: var(--text-muted);
  font-style: italic;
}

.thinking-section {
  margin: 8px 0 12px;
}

.thinking-toggle {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  background: transparent;
  border: 1px dashed var(--border-color);
  color: var(--text-muted);
  padding: 4px 10px;
  border-radius: var(--radius);
  cursor: pointer;
  font-size: 12px;
}

.thinking-toggle:hover {
  color: var(--text-secondary);
  border-color: var(--text-muted);
}

.thinking-icon {
  font-size: 9px;
}

.thinking-count {
  color: var(--accent);
}

.thinking-body {
  margin-top: 8px;
  padding: 8px 10px;
  border-left: 2px solid var(--border-color);
  background: rgba(255, 255, 255, 0.015);
  border-radius: 0 var(--radius) var(--radius) 0;
}
</style>
