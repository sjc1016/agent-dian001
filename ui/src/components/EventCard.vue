<script setup>
import { ref, computed } from 'vue'
import { eventCategory, categoryClass, categoryColor } from '../utils/events.js'

const props = defineProps({
  title: { type: String, required: true },
  body: { type: String, default: '' },
  detail: { type: String, default: '' },
  category: { type: String, default: 'info' },
  collapsed: { type: Boolean, default: true },
})

const expanded = ref(false)
const summary = computed(() => ({
  title: props.title,
  body: props.body,
  category: props.category,
}))
const displayCategory = computed(() => eventCategory(summary.value))
const detailText = computed(() => props.detail || props.body)
const cardClass = computed(() => categoryClass(displayCategory.value))
const titleColor = computed(() => categoryColor(displayCategory.value))
const marker = computed(() => {
  const map = { running: '•', success: '✓', error: '!', info: '·', user: '>' }
  return map[displayCategory.value] || '·'
})
const expandIcon = computed(() => expanded.value ? '▼' : '▶')

function toggle() {
  expanded.value = !expanded.value
}

function tryFormatJson(text) {
  if (!text) return text
  if (text.length > 20000) return text.slice(0, 19997) + '...'
  try {
    return JSON.stringify(JSON.parse(text), null, 2)
  } catch {
    return text
  }
}
</script>

<template>
  <div class="event-card" :class="[cardClass, { expanded }]">
    <div class="event-header" @click="toggle">
      <span class="event-marker" :style="{ color: titleColor }">{{ marker }}</span>
      <span class="event-title" :style="{ color: titleColor }">{{ title }}</span>
      <span class="event-expand-icon">{{ expandIcon }}</span>
    </div>
    <div v-if="expanded" class="event-body">
      <div class="event-detail">
        <pre>{{ tryFormatJson(detailText) }}</pre>
      </div>
    </div>
  </div>
</template>

<style scoped>
.event-card {
  margin-bottom: 8px;
  padding: 8px 12px;
  border-left: 3px solid var(--border-color);
  background: var(--bg-primary);
  border-radius: 0 4px 4px 0;
}

.event-running { border-left-color: var(--warning); }
.event-success { border-left-color: var(--success); }
.event-error { border-left-color: var(--error); }
.event-info { border-left-color: var(--accent); }
.event-user {
  border-left-color: var(--warning);
  background: var(--bg-tertiary);
}

.event-header {
  display: flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
  user-select: none;
}

.event-marker {
  font-weight: bold;
}

.event-title {
  font-weight: 600;
  flex: 1;
}

.event-expand-icon {
  color: var(--text-subtle);
  font-size: 10px;
  margin-left: 8px;
}

.event-body {
  margin-top: 8px;
}

.event-detail {
  padding: 8px;
  background: var(--bg-secondary);
  border-radius: var(--radius);
  color: var(--text-secondary);
  white-space: pre-wrap;
  word-break: break-word;
}

.event-detail pre {
  white-space: pre-wrap;
  word-break: break-word;
  font-size: 12px;
}
</style>
