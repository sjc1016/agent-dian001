<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import {
  listTables,
  getTableSchema,
  listRows,
  createRow,
  updateRow,
  deleteRow,
  getRagStats,
  getRagSources,
  getRagSourceDetail,
  ingestRag,
  uploadRag,
  deleteRagSource,
} from '../api/admin.js'

const emit = defineEmits(['close'])

const tab = ref('db')

/* ---------------- 数据库管理 ---------------- */
const tables = ref([])
const activeTable = ref('')
const schema = reactive({ columns: [], pkColumns: [] })
const rowsData = reactive({ rows: [], columns: [], total: 0, limit: 20, offset: 0 })
const keyword = ref('')
const dbLoading = ref(false)
const dbError = ref('')
const dbNotice = ref('')

// 行编辑表单（新增 / 编辑共用）
const editorVisible = ref(false)
const editorMode = ref('create')
const editorForm = reactive({})
const editorOldPk = ref('')
const editorError = ref('')
const editorSaving = ref(false)

const pageCount = computed(() =>
  Math.max(1, Math.ceil(rowsData.total / rowsData.limit))
)
const currentPage = computed(() => Math.floor(rowsData.offset / rowsData.limit) + 1)

function flash(message, isError = false) {
  if (isError) {
    dbError.value = message
    dbNotice.value = ''
  } else {
    dbNotice.value = message
    dbError.value = ''
  }
  setTimeout(() => {
    if (dbError.value === message) dbError.value = ''
    if (dbNotice.value === message) dbNotice.value = ''
  }, 3200)
}

async function loadTables() {
  try {
    const data = await listTables()
    tables.value = data.tables || []
    if (!activeTable.value && tables.value.length) {
      await selectTable(tables.value[0])
    }
  } catch (err) {
    flash(err.message, true)
  }
}

async function selectTable(name) {
  activeTable.value = name
  keyword.value = ''
  rowsData.offset = 0
  try {
    const data = await getTableSchema(name)
    schema.columns = data.columns || []
    schema.pkColumns = data.pk_columns || []
  } catch (err) {
    flash(err.message, true)
    return
  }
  await loadRows()
}

async function loadRows(resetOffset = false) {
  if (!activeTable.value) return
  if (resetOffset) rowsData.offset = 0
  dbLoading.value = true
  try {
    const data = await listRows(activeTable.value, {
      limit: rowsData.limit,
      offset: rowsData.offset,
      q: keyword.value.trim(),
    })
    rowsData.rows = data.rows || []
    rowsData.columns = data.columns || []
    rowsData.total = data.total || 0
  } catch (err) {
    flash(err.message, true)
  } finally {
    dbLoading.value = false
  }
}

function prevPage() {
  if (rowsData.offset === 0) return
  rowsData.offset = Math.max(0, rowsData.offset - rowsData.limit)
  loadRows()
}

function nextPage() {
  if (rowsData.offset + rowsData.limit >= rowsData.total) return
  rowsData.offset += rowsData.limit
  loadRows()
}

/** 主键值：复合主键用 `|` 连接，与后端约定一致。 */
function pkValue(row) {
  return schema.pkColumns.map((name) => row[name] ?? '').join('|')
}

function isPkColumn(name) {
  return schema.pkColumns.includes(name)
}

function openCreate() {
  editorMode.value = 'create'
  editorOldPk.value = ''
  editorError.value = ''
  for (const col of schema.columns) {
    editorForm[col.name] = ''
  }
  editorVisible.value = true
}

function openEdit(row) {
  editorMode.value = 'edit'
  editorOldPk.value = pkValue(row)
  editorError.value = ''
  for (const col of schema.columns) {
    const value = row[col.name]
    editorForm[col.name] = value === null || value === undefined ? '' : String(value)
  }
  editorVisible.value = true
}

async function saveEditor() {
  editorError.value = ''
  const payload = {}
  for (const col of schema.columns) {
    const raw = editorForm[col.name]
    // 新增时留空的可选列不下发，交给数据库默认值处理
    if (editorMode.value === 'create' && raw === '') continue
    // 主键为空的新增行同样跳过，交由默认值
    if (editorMode.value === 'create' && isPkColumn(col.name) && raw === '') continue
    payload[col.name] = raw
  }
  if (!Object.keys(payload).length) {
    editorError.value = '请至少填写一个字段。'
    return
  }
  editorSaving.value = true
  try {
    if (editorMode.value === 'create') {
      await createRow(activeTable.value, payload)
      flash('新增成功')
    } else {
      await updateRow(activeTable.value, editorOldPk.value, payload)
      flash('更新成功')
    }
    editorVisible.value = false
    await loadRows()
  } catch (err) {
    editorError.value = err.message
  } finally {
    editorSaving.value = false
  }
}

async function removeRow(row) {
  const pk = pkValue(row)
  if (!window.confirm(`确认删除该行？\n主键：${pk || '(无)'}`)) return
  try {
    await deleteRow(activeTable.value, pk)
    flash('删除成功')
    await loadRows()
  } catch (err) {
    flash(err.message, true)
  }
}

/* ---------------- RAG 知识库管理 ---------------- */
const ragStats = reactive({ chunk_count: 0, milvus_count: 0 })
const sources = ref([])
const ragLoading = ref(false)
const ragError = ref('')
const ragNotice = ref('')
const ingestPath = ref('')
const uploadFile = ref(null)
const ingesting = ref(false)

// 分片预览弹窗
const previewVisible = ref(false)
const previewLoading = ref(false)
const previewError = ref('')
const previewTab = ref('parent')
const previewData = reactive({
  doc_source: '',
  child_count: 0,
  parent_count: 0,
  children: [],
  parents: [],
})

async function openPreview(source) {
  previewVisible.value = true
  previewTab.value = 'parent'
  previewError.value = ''
  previewLoading.value = true
  previewData.doc_source = source
  previewData.children = []
  previewData.parents = []
  previewData.child_count = 0
  previewData.parent_count = 0
  try {
    const data = await getRagSourceDetail(source)
    previewData.doc_source = data.doc_source
    previewData.child_count = data.child_count
    previewData.parent_count = data.parent_count
    previewData.children = data.children || []
    previewData.parents = data.parents || []
  } catch (err) {
    previewError.value = err.message
  } finally {
    previewLoading.value = false
  }
}

function ragFlash(message, isError = false) {
  if (isError) {
    ragError.value = message
    ragNotice.value = ''
  } else {
    ragNotice.value = message
    ragError.value = ''
  }
  setTimeout(() => {
    if (ragError.value === message) ragError.value = ''
    if (ragNotice.value === message) ragNotice.value = ''
  }, 4200)
}

async function loadRag() {
  ragLoading.value = true
  try {
    const [stats, sourceData] = await Promise.all([getRagStats(), getRagSources()])
    ragStats.chunk_count = stats.chunk_count || 0
    ragStats.milvus_count = stats.milvus_count || 0
    sources.value = sourceData.sources || []
  } catch (err) {
    ragFlash(err.message, true)
  } finally {
    ragLoading.value = false
  }
}

async function runIngest(all = false) {
  if (ingesting.value) return
  if (!all && !ingestPath.value.trim()) {
    ragFlash('请输入待入库的文件或目录路径，或点击「全量入库」。', true)
    return
  }
  ingesting.value = true
  try {
    const result = await ingestRag({ path: all ? '' : ingestPath.value.trim(), all })
    const count = result.total_children ?? 0
    const errors = result.errors || []
    ragFlash(
      `入库完成：${count} 个分片${errors.length ? `，${errors.length} 个文档失败` : ''}`,
      errors.length > 0 && count === 0
    )
    await loadRag()
  } catch (err) {
    ragFlash(err.message, true)
  } finally {
    ingesting.value = false
  }
}

async function runUpload() {
  if (ingesting.value || !uploadFile.value) {
    if (!uploadFile.value) ragFlash('请选择要上传的文档。', true)
    return
  }
  ingesting.value = true
  try {
    const result = await uploadRag(uploadFile.value)
    ragFlash(`上传并入库完成：${result.total_children ?? 0} 个分片`)
    uploadFile.value = null
    const input = document.getElementById('rag-file-input')
    if (input) input.value = ''
    await loadRag()
  } catch (err) {
    ragFlash(err.message, true)
  } finally {
    ingesting.value = false
  }
}

function onFileChange(event) {
  uploadFile.value = event.target.files?.[0] || null
}

async function removeSource(source) {
  if (!window.confirm(`确认删除知识来源「${source}」？\n将同时删除其 SQLite 元信息与 Milvus 向量。`)) return
  try {
    const result = await deleteRagSource(source)
    ragFlash(`已删除 ${result.deleted ?? 0} 个分片`)
    // 删掉的来源若正在预览，一并关闭弹窗，避免展示已失效数据
    if (previewVisible.value && previewData.doc_source === source) {
      previewVisible.value = false
    }
    await loadRag()
  } catch (err) {
    ragFlash(err.message, true)
  }
}

onMounted(async () => {
  await Promise.all([loadTables(), loadRag()])
})
</script>

<template>
  <div class="admin-overlay">
    <div class="admin-shell">
      <header class="admin-header">
        <div class="header-text">
          <h2>后台管理</h2>
          <span class="header-sub">数据库管理 · RAG 知识库管理</span>
        </div>
        <button class="close-btn" @click="emit('close')">返回对话</button>
      </header>

      <nav class="tabs">
        <button class="tab" :class="{ active: tab === 'db' }" @click="tab = 'db'">
          后台数据库管理
        </button>
        <button class="tab" :class="{ active: tab === 'rag' }" @click="tab = 'rag'">
          RAG 知识库管理
        </button>
      </nav>

      <!-- 数据库管理 -->
      <div v-if="tab === 'db'" class="admin-body db-body">
        <aside class="table-list">
          <div class="pane-title">数据表 ({{ tables.length }})</div>
          <button
            v-for="name in tables"
            :key="name"
            class="table-item"
            :class="{ active: name === activeTable }"
            @click="selectTable(name)"
          >
            {{ name }}
          </button>
        </aside>

        <section class="table-main">
          <div class="toolbar">
            <input
              v-model="keyword"
              class="input search"
              type="text"
              placeholder="搜索当前表任意字段..."
              @keyup.enter="loadRows(true)"
            />
            <button class="btn" @click="loadRows(true)">查询</button>
            <button class="btn" @click="keyword = ''; loadRows(true)">重置</button>
            <button class="btn primary" @click="openCreate">新增一行</button>
          </div>

          <div v-if="dbError" class="alert error">{{ dbError }}</div>
          <div v-else-if="dbNotice" class="alert success">{{ dbNotice }}</div>

          <div class="grid-wrap">
            <div v-if="dbLoading" class="placeholder">加载中...</div>
            <div v-else-if="!rowsData.rows.length" class="placeholder">暂无数据</div>
            <table v-else class="grid">
              <thead>
                <tr>
                  <th v-for="col in rowsData.columns" :key="col">
                    {{ col }}<span v-if="isPkColumn(col)" class="pk-tag">PK</span>
                  </th>
                  <th class="op-col">操作</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(row, index) in rowsData.rows" :key="index">
                  <td v-for="col in rowsData.columns" :key="col" :title="String(row[col] ?? '')">
                    {{ row[col] === null || row[col] === undefined ? '' : row[col] }}
                  </td>
                  <td class="op-col">
                    <button class="link-btn" @click="openEdit(row)">编辑</button>
                    <button class="link-btn danger" @click="removeRow(row)">删除</button>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>

          <div class="pager">
            <span class="pager-info">
              共 {{ rowsData.total }} 行 · 第 {{ currentPage }}/{{ pageCount }} 页
            </span>
            <div class="pager-actions">
              <button class="btn" :disabled="rowsData.offset === 0" @click="prevPage">上一页</button>
              <button
                class="btn"
                :disabled="rowsData.offset + rowsData.limit >= rowsData.total"
                @click="nextPage"
              >下一页</button>
            </div>
          </div>
        </section>
      </div>

      <!-- RAG 知识库管理 -->
      <div v-else class="admin-body rag-body">
        <div v-if="ragError" class="alert error">{{ ragError }}</div>
        <div v-else-if="ragNotice" class="alert success">{{ ragNotice }}</div>

        <div class="stat-cards">
          <div class="stat-card">
            <span class="stat-label">SQLite 分片</span>
            <span class="stat-value">{{ ragStats.chunk_count }}</span>
          </div>
          <div class="stat-card">
            <span class="stat-label">Milvus 向量</span>
            <span class="stat-value">{{ ragStats.milvus_count }}</span>
          </div>
          <div class="stat-card">
            <span class="stat-label">知识来源</span>
            <span class="stat-value">{{ sources.length }}</span>
          </div>
        </div>

        <div class="rag-actions">
          <div class="action-block">
            <div class="pane-title">入库执行</div>
            <div class="action-row">
              <input
                v-model="ingestPath"
                class="input"
                type="text"
                placeholder="文件或目录路径（相对 knowledge/），留空则配合全量入库"
              />
              <button class="btn" :disabled="ingesting" @click="runIngest(false)">
                {{ ingesting ? '执行中...' : '按路径入库' }}
              </button>
              <button class="btn primary" :disabled="ingesting" @click="runIngest(true)">
                全量入库
              </button>
            </div>
            <div class="action-row">
              <input
                id="rag-file-input"
                class="input file-input"
                type="file"
                @change="onFileChange"
              />
              <button class="btn" :disabled="ingesting || !uploadFile" @click="runUpload">
                上传并入库
              </button>
            </div>
            <p class="hint">按路径/全量入库会先清理同来源旧向量与元信息，再重新解析、分片、向量化写入。</p>
          </div>
        </div>

        <div class="source-panel">
          <div class="pane-title">
            知识来源 ({{ sources.length }})
            <button class="btn small" :disabled="ragLoading" @click="loadRag">刷新</button>
          </div>
          <p class="hint">点击任意来源行可预览其全部分片（父分片 / 子分片）。</p>
          <div class="grid-wrap">
            <div v-if="ragLoading" class="placeholder">加载中...</div>
            <div v-else-if="!sources.length" class="placeholder">知识库为空，请先执行入库</div>
            <table v-else class="grid">
              <thead>
                <tr>
                  <th>来源文档</th>
                  <th class="num-col">分片数</th>
                  <th class="op-col">操作</th>
                </tr>
              </thead>
              <tbody>
                <tr
                  v-for="item in sources"
                  :key="item.doc_source"
                  class="clickable-row"
                  @click="openPreview(item.doc_source)"
                >
                  <td :title="item.doc_source" class="source-cell">{{ item.doc_source }}</td>
                  <td class="num-col">{{ item.chunk_count }}</td>
                  <td class="op-col">
                    <button class="link-btn" @click.stop="openPreview(item.doc_source)">查看分片</button>
                    <button class="link-btn danger" @click.stop="removeSource(item.doc_source)">删除</button>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>

    <!-- 行编辑弹窗 -->
    <div v-if="editorVisible" class="modal-overlay" @click.self="editorVisible = false">
      <div class="editor-modal">
        <div class="modal-header">
          <h3>{{ editorMode === 'create' ? '新增数据行' : '编辑数据行' }}</h3>
          <button class="close-btn" @click="editorVisible = false">×</button>
        </div>
        <div class="editor-body">
          <label v-for="col in schema.columns" :key="col.name" class="field">
            <span class="field-label">
              {{ col.name }}
              <span class="field-type">{{ col.type }}</span>
              <span v-if="isPkColumn(col.name)" class="pk-tag">PK</span>
            </span>
            <input
              v-model="editorForm[col.name]"
              class="input"
              type="text"
              :placeholder="col.dflt_value ? `默认 ${col.dflt_value}` : ''"
            />
          </label>
          <div v-if="editorError" class="alert error">{{ editorError }}</div>
        </div>
        <div class="modal-footer">
          <span class="footer-hint">表：{{ activeTable }}</span>
          <div class="footer-actions">
            <button class="btn" @click="editorVisible = false">取消</button>
            <button class="btn primary" :disabled="editorSaving" @click="saveEditor">
              {{ editorSaving ? '保存中...' : '保存' }}
            </button>
          </div>
        </div>
      </div>
    </div>

    <!-- 分片预览弹窗 -->
    <div v-if="previewVisible" class="modal-overlay" @click.self="previewVisible = false">
      <div class="preview-modal">
        <div class="modal-header">
          <div class="preview-title">
            <h3>分片预览</h3>
            <span class="preview-source" :title="previewData.doc_source">
              {{ previewData.doc_source }}
            </span>
          </div>
          <button class="close-btn" @click="previewVisible = false">×</button>
        </div>

        <div class="preview-tabs">
          <button
            class="preview-tab"
            :class="{ active: previewTab === 'parent' }"
            @click="previewTab = 'parent'"
          >
            父分片 ({{ previewData.parent_count }})
          </button>
          <button
            class="preview-tab"
            :class="{ active: previewTab === 'child' }"
            @click="previewTab = 'child'"
          >
            子分片 ({{ previewData.child_count }})
          </button>
        </div>

        <div class="preview-body">
          <div v-if="previewLoading" class="placeholder">加载中...</div>
          <div v-else-if="previewError" class="alert error">{{ previewError }}</div>

          <template v-else-if="previewTab === 'parent'">
            <div v-if="!previewData.parents.length" class="placeholder">暂无父分片</div>
            <div
              v-for="(item, index) in previewData.parents"
              :key="item.parent_id"
              class="chunk-card"
            >
              <div class="chunk-head">
                <span class="chunk-index">#{{ index + 1 }}</span>
                <span class="chunk-meta">position {{ item.position }}</span>
                <span class="chunk-meta">含 {{ item.child_count }} 个子分片</span>
                <span class="chunk-meta">{{ item.char_count }} 字</span>
                <span class="chunk-id" :title="item.parent_id">{{ item.parent_id }}</span>
              </div>
              <pre class="chunk-text">{{ item.text }}</pre>
            </div>
          </template>

          <template v-else>
            <div v-if="!previewData.children.length" class="placeholder">暂无子分片</div>
            <div
              v-for="(item, index) in previewData.children"
              :key="item.child_id"
              class="chunk-card"
            >
              <div class="chunk-head">
                <span class="chunk-index">#{{ index + 1 }}</span>
                <span class="chunk-meta">position {{ item.position }}</span>
                <span class="chunk-meta">{{ item.char_count }} 字</span>
                <span class="chunk-id" :title="item.parent_id">父 {{ item.parent_id }}</span>
              </div>
              <pre class="chunk-text">{{ item.text }}</pre>
            </div>
          </template>
        </div>

        <div class="modal-footer">
          <span class="footer-hint">
            父 {{ previewData.parent_count }} · 子 {{ previewData.child_count }}
          </span>
          <div class="footer-actions">
            <button class="btn" @click="previewVisible = false">关闭</button>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.admin-overlay {
  position: fixed;
  inset: 0;
  z-index: 130;
  background: rgba(6, 7, 8, 0.88);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
}

.admin-shell {
  width: 100%;
  height: 100%;
  max-width: 1400px;
  display: flex;
  flex-direction: column;
  background: var(--bg-primary);
  border: 1px solid var(--border-color);
  border-radius: var(--radius);
  overflow: hidden;
}

.admin-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-shrink: 0;
  padding: 14px 20px;
  background: var(--bg-secondary);
  border-bottom: 1px solid var(--border-color);
}

.header-text {
  display: flex;
  align-items: baseline;
  gap: 12px;
}

.admin-header h2 {
  margin: 0;
  color: var(--warning);
  font-size: 17px;
}

.header-sub {
  color: var(--text-subtle);
  font-size: 12px;
}

.tabs {
  display: flex;
  flex-shrink: 0;
  background: var(--bg-secondary);
  border-bottom: 1px solid var(--border-color);
  padding: 0 12px;
}

.tab {
  padding: 10px 18px;
  background: transparent;
  border: none;
  border-bottom: 2px solid transparent;
  color: var(--text-muted);
  font-size: 13px;
  cursor: pointer;
}

.tab:hover {
  color: var(--text-secondary);
}

.tab.active {
  color: var(--accent);
  border-bottom-color: var(--accent);
  font-weight: 600;
}

.admin-body {
  flex: 1;
  min-height: 0;
  display: flex;
  padding: 14px;
  gap: 14px;
}

/* 数据库管理：左表列表 + 右数据区 */
.db-body {
  flex-direction: row;
}

.table-list {
  width: 200px;
  flex-shrink: 0;
  overflow-y: auto;
  background: var(--bg-secondary);
  border: 1px solid var(--border-color);
  border-radius: var(--radius);
  padding: 8px;
}

.pane-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 8px;
  color: var(--warning);
  font-size: 12px;
  font-weight: 700;
}

.table-item {
  display: block;
  width: 100%;
  text-align: left;
  padding: 7px 9px;
  margin-bottom: 4px;
  background: transparent;
  border: 1px solid transparent;
  border-radius: var(--radius);
  color: var(--text-secondary);
  font-size: 12px;
  font-family: "SF Mono", Consolas, Monaco, monospace;
  cursor: pointer;
}

.table-item:hover {
  background: var(--bg-tertiary);
}

.table-item.active {
  background: var(--bg-tertiary);
  border-color: var(--accent-dim);
  color: var(--accent);
}

.table-main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  background: var(--bg-secondary);
  border: 1px solid var(--border-color);
  border-radius: var(--radius);
  padding: 12px;
}

.toolbar {
  display: flex;
  gap: 8px;
  margin-bottom: 10px;
}

.input {
  padding: 7px 10px;
  background: var(--bg-tertiary);
  border: 1px solid var(--border-color);
  border-radius: var(--radius);
  color: var(--text-primary);
  font-size: 12px;
  font-family: inherit;
}

.input:focus {
  outline: none;
  border-color: var(--accent-dim);
}

.search {
  flex: 1;
  min-width: 0;
}

.btn {
  padding: 7px 14px;
  background: transparent;
  border: 1px solid var(--border-color);
  border-radius: var(--radius);
  color: var(--text-secondary);
  font-size: 12px;
  cursor: pointer;
  white-space: nowrap;
}

.btn:hover:not(:disabled) {
  background: var(--bg-tertiary);
}

.btn:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

.btn.primary {
  background: var(--accent-dim);
  border-color: var(--accent-dim);
  color: var(--text-primary);
  font-weight: 600;
}

.btn.primary:hover:not(:disabled) {
  background: var(--accent);
  border-color: var(--accent);
  color: var(--bg-primary);
}

.btn.small {
  padding: 3px 9px;
  font-size: 11px;
}

.alert {
  padding: 7px 10px;
  margin-bottom: 8px;
  border-radius: var(--radius);
  font-size: 12px;
}

.alert.error {
  background: rgba(239, 111, 108, 0.12);
  border: 1px solid var(--error);
  color: var(--error);
}

.alert.success {
  background: rgba(127, 214, 138, 0.1);
  border: 1px solid var(--success);
  color: var(--success);
}

.grid-wrap {
  flex: 1;
  min-height: 0;
  overflow: auto;
  border: 1px solid var(--border-color);
  border-radius: var(--radius);
}

.grid {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}

.grid th,
.grid td {
  padding: 7px 10px;
  text-align: left;
  border-bottom: 1px solid var(--border-color);
  white-space: nowrap;
  max-width: 260px;
  overflow: hidden;
  text-overflow: ellipsis;
}

.grid th {
  position: sticky;
  top: 0;
  background: var(--bg-tertiary);
  color: var(--accent);
  font-weight: 600;
  z-index: 1;
}

.grid tbody tr:hover {
  background: var(--bg-tertiary);
}

.grid td {
  color: var(--text-secondary);
  font-family: "SF Mono", Consolas, Monaco, monospace;
}

.op-col {
  width: 150px;
}

.num-col {
  width: 90px;
}

.pk-tag {
  margin-left: 5px;
  padding: 0 4px;
  background: var(--accent-dim);
  border-radius: 3px;
  color: var(--text-primary);
  font-size: 9px;
  font-weight: 700;
}

.link-btn {
  background: transparent;
  border: none;
  color: var(--accent);
  font-size: 12px;
  cursor: pointer;
  padding: 2px 5px;
}

.link-btn:hover {
  text-decoration: underline;
}

.link-btn.danger {
  color: var(--error);
}

.placeholder {
  padding: 36px;
  text-align: center;
  color: var(--text-subtle);
  font-size: 13px;
}

.pager {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-shrink: 0;
  padding-top: 10px;
}

.pager-info {
  color: var(--text-muted);
  font-size: 12px;
}

.pager-actions {
  display: flex;
  gap: 8px;
}

/* RAG 管理 */
.rag-body {
  flex-direction: column;
  overflow-y: auto;
}

.stat-cards {
  display: flex;
  gap: 12px;
  flex-shrink: 0;
}

.stat-card {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 12px 14px;
  background: var(--bg-secondary);
  border: 1px solid var(--border-color);
  border-radius: var(--radius);
}

.stat-label {
  color: var(--text-muted);
  font-size: 12px;
}

.stat-value {
  color: var(--accent);
  font-size: 22px;
  font-weight: 700;
}

.rag-actions,
.source-panel {
  flex-shrink: 0;
  background: var(--bg-secondary);
  border: 1px solid var(--border-color);
  border-radius: var(--radius);
  padding: 12px;
}

.action-block {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.action-row {
  display: flex;
  gap: 8px;
  align-items: center;
}

.action-row .input {
  flex: 1;
  min-width: 0;
}

.file-input {
  padding: 5px 8px;
}

.hint {
  margin: 0;
  color: var(--text-subtle);
  font-size: 11px;
}

.source-panel {
  display: flex;
  flex-direction: column;
  min-height: 200px;
}

.source-panel .grid-wrap {
  max-height: 340px;
}

/* 来源行可点击预览分片 */
.clickable-row {
  cursor: pointer;
}

.source-cell {
  color: var(--accent);
}

.clickable-row:hover .source-cell {
  text-decoration: underline;
}

/* 分片预览弹窗 */
.preview-modal {
  width: 760px;
  max-width: 94vw;
  height: 82vh;
  display: flex;
  flex-direction: column;
  background: var(--bg-secondary);
  border: 1px solid var(--border-color);
  border-radius: var(--radius);
  box-shadow: var(--shadow);
  overflow: hidden;
}

.preview-title {
  display: flex;
  flex-direction: column;
  gap: 3px;
  min-width: 0;
}

.preview-title h3 {
  margin: 0;
  color: var(--accent);
  font-size: 15px;
}

.preview-source {
  max-width: 620px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-subtle);
  font-size: 11px;
  font-family: "SF Mono", Consolas, Monaco, monospace;
}

.preview-tabs {
  display: flex;
  flex-shrink: 0;
  gap: 4px;
  padding: 8px 16px 0;
  border-bottom: 1px solid var(--border-color);
}

.preview-tab {
  padding: 7px 14px;
  background: transparent;
  border: none;
  border-bottom: 2px solid transparent;
  color: var(--text-muted);
  font-size: 12px;
  cursor: pointer;
}

.preview-tab:hover {
  color: var(--text-secondary);
}

.preview-tab.active {
  color: var(--accent);
  border-bottom-color: var(--accent);
  font-weight: 600;
}

.preview-body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 12px 16px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.chunk-card {
  flex-shrink: 0;
  background: var(--bg-tertiary);
  border: 1px solid var(--border-color);
  border-radius: var(--radius);
  overflow: hidden;
}

.chunk-head {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
  padding: 7px 10px;
  background: rgba(255, 255, 255, 0.02);
  border-bottom: 1px solid var(--border-color);
}

.chunk-index {
  color: var(--accent);
  font-size: 12px;
  font-weight: 700;
}

.chunk-meta {
  color: var(--text-muted);
  font-size: 11px;
}

.chunk-id {
  margin-left: auto;
  max-width: 340px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-subtle);
  font-size: 10px;
  font-family: "SF Mono", Consolas, Monaco, monospace;
}

.chunk-text {
  margin: 0;
  padding: 10px;
  max-height: 300px;
  overflow: auto;
  color: var(--text-secondary);
  font-size: 12px;
  font-family: inherit;
  line-height: 1.7;
  white-space: pre-wrap;
  word-break: break-word;
}

/* 编辑弹窗 */
.modal-overlay {
  position: fixed;
  inset: 0;
  z-index: 150;
  background: rgba(0, 0, 0, 0.7);
  display: flex;
  align-items: center;
  justify-content: center;
}

.editor-modal {
  width: 460px;
  max-width: 92vw;
  max-height: 88vh;
  display: flex;
  flex-direction: column;
  background: var(--bg-secondary);
  border: 1px solid var(--border-color);
  border-radius: var(--radius);
  box-shadow: var(--shadow);
  overflow: hidden;
}

.modal-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-shrink: 0;
  padding: 14px 16px;
  border-bottom: 1px solid var(--border-color);
}

.modal-header h3 {
  margin: 0;
  color: var(--accent);
  font-size: 15px;
}

.editor-body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 14px 16px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.field {
  display: flex;
  flex-direction: column;
  gap: 5px;
}

.field-label {
  display: flex;
  align-items: center;
  color: var(--text-muted);
  font-size: 12px;
}

.field-type {
  margin-left: 6px;
  color: var(--text-subtle);
  font-size: 10px;
}

.modal-footer {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-shrink: 0;
  gap: 12px;
  padding: 12px 16px;
  border-top: 1px solid var(--border-color);
}

.footer-hint {
  color: var(--text-subtle);
  font-size: 11px;
  font-family: "SF Mono", Consolas, Monaco, monospace;
}

.footer-actions {
  display: flex;
  gap: 8px;
}

.close-btn {
  padding: 6px 12px;
  background: transparent;
  border: 1px solid var(--border-color);
  border-radius: var(--radius);
  color: var(--text-secondary);
  font-size: 12px;
  cursor: pointer;
}

.close-btn:hover {
  background: var(--bg-tertiary);
}
</style>
