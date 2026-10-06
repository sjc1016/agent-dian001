<script setup>
import { ref, watch } from 'vue'

const props = defineProps({
  request: { type: Object, default: null },
})

const emit = defineEmits(['resolve'])

const reason = ref('')

watch(() => props.request, (req) => {
  reason.value = req?.risk_reason || ''
})

function approve() {
  emit('resolve', { approved: true })
}

function deny() {
  emit('resolve', { approved: false })
}

function close() {
  emit('resolve', { approved: false })
}
</script>

<template>
  <div v-if="request" class="modal-overlay" @click.self="close">
    <div class="modal">
      <div class="modal-header">
        <h3>Approval Required</h3>
        <button class="close-btn" @click="close">×</button>
      </div>
      <div class="modal-body">
        <div class="field">
          <span class="label">tool:</span>
          <span class="value">{{ request.name || 'unknown' }}</span>
        </div>
        <div class="field">
          <span class="label">args:</span>
          <pre class="code">{{ JSON.stringify(request.args || {}, null, 2) }}</pre>
        </div>
        <div v-if="reason" class="field">
          <span class="label">risk:</span>
          <span class="value warn">{{ reason }}</span>
        </div>
      </div>
      <div class="modal-footer">
        <button class="deny-btn" @click="deny">Deny</button>
        <button class="approve-btn" @click="approve">Approve</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.modal-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.7);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 100;
}

.modal {
  width: 480px;
  max-width: 90vw;
  background: var(--bg-secondary);
  border: 1px solid var(--border-color);
  border-radius: var(--radius);
  box-shadow: var(--shadow);
}

.modal-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 16px;
  border-bottom: 1px solid var(--border-color);
}

.modal-header h3 {
  margin: 0;
  color: var(--warning);
}

.close-btn {
  background: transparent;
  border: none;
  color: var(--text-muted);
  font-size: 20px;
  cursor: pointer;
}

.modal-body {
  padding: 16px;
}

.field {
  margin-bottom: 12px;
}

.label {
  color: var(--accent);
  font-weight: 600;
  margin-right: 8px;
}

.value {
  color: var(--text-secondary);
}

.warn {
  color: var(--error);
}

.code {
  margin-top: 8px;
  padding: 10px;
  background: var(--bg-primary);
  border-radius: var(--radius);
  color: var(--text-secondary);
  font-size: 12px;
  max-height: 200px;
  overflow: auto;
}

.modal-footer {
  display: flex;
  justify-content: flex-end;
  gap: 12px;
  padding: 16px;
  border-top: 1px solid var(--border-color);
}

.approve-btn {
  background: var(--success);
  color: var(--bg-primary);
  border: none;
  padding: 8px 16px;
  border-radius: var(--radius);
  cursor: pointer;
  font-weight: 600;
}

.deny-btn {
  background: var(--error);
  color: var(--bg-primary);
  border: none;
  padding: 8px 16px;
  border-radius: var(--radius);
  cursor: pointer;
  font-weight: 600;
}
</style>
