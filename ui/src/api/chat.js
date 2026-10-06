const API_BASE = import.meta.env.VITE_API_BASE || ''

export function streamChat({ message, workspace, sessionId, phone, approvalMode = 'inline', maxAttempts = 3, onEvent, onError, onDone }) {
  const body = {
    message,
    workspace,
    session_id: sessionId,
    phone,
    approval_mode: approvalMode,
    max_attempts: maxAttempts,
  }

  const controller = new AbortController()

  fetch(`${API_BASE}/api/v1/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal: controller.signal,
  })
    .then((response) => {
      if (!response.ok || !response.body) {
        throw new Error(`HTTP ${response.status}: ${response.statusText}`)
      }
      const reader = response.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''

      function read() {
        reader.read().then(({ done, value }) => {
          if (done) {
            onDone?.()
            return
          }
          buffer += decoder.decode(value, { stream: true })
          const lines = buffer.split('\n\n')
          buffer = lines.pop() || ''
          for (const chunk of lines) {
            const dataMatch = chunk.match(/^data: (.+)$/m)
            if (!dataMatch) continue
            const raw = dataMatch[1].trim()
            if (raw === '[DONE]') {
              onDone?.()
              return
            }
            try {
              const event = JSON.parse(raw)
              onEvent?.(event)
            } catch (err) {
              onEvent?.({ type: 'raw', data: raw })
            }
          }
          read()
        }).catch((err) => {
          if (err.name !== 'AbortError') onError?.(err)
        })
      }

      read()
    })
    .catch((err) => {
      if (err.name !== 'AbortError') onError?.(err)
    })

  return controller
}
