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
  getFaqStats,
  listFaq,
  getFaqEntry,
  updateFaqStatus,
  reflowFaq,
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

/* ---------------- FAQ 沉淀库管理 ---------------- */
const FAQ_STATUS_LABELS = {
  draft: '待审核',
  approved: '已通过',
  published: '已发布',
  archived: '已归档',
}

const faqStats = reactive({ total: 0, pending_review: 0, published: 0, by_category: {} })
const faqRows = ref([])
const faqTotal = ref(0)
const faqFilters = reactive({ status: '', category: '', q: '' })
const faqLoading = ref(false)
const faqReflowing = ref(false)
const faqError = ref('')
const faqNotice = ref('')
const faqPageSize = 20

// 条目详情弹窗（预览解决方案全文与来源）
const faqDetailVisible = ref(false)
const faqDetailLoading = ref(false)
const faqDetailError = ref('')
const faqDetail = ref(null)

function faqFlash(message, isError = false) {
  if (isError) {
    faqError.value = message
    faqNotice.value = ''
  } else {
    faqNotice.value = message
    faqError.value = ''
  }
  setTimeout(() => {
    if (faqError.value === message) faqError.value = ''
    if (faqNotice.value === message) faqNotice.value = ''
  }, 4200)
}

function statusLabel(status) {
  return FAQ_STATUS_LABELS[status] || status
}

async function loadFaq() {
  faqLoading.value = true
  try {
    const [stats, listing] = await Promise.all([
      getFaqStats(),
      listFaq({
        status: faqFilters.status,
        category: faqFilters.category,
        q: faqFilters.q.trim(),
        limit: faqPageSize,
      }),
    ])
    faqStats.total = stats.total || 0
    faqStats.pending_review = stats.pending_review || 0
    faqStats.published = stats.published || 0
    faqStats.by_category = stats.by_category || {}
    faqRows.value = listing.rows || []
    faqTotal.value = listing.total || 0
  } catch (err) {
    faqFlash(err.message, true)
  } finally {
    faqLoading.value = false
  }
}

async function changeFaqStatus(row, status) {
  const label = statusLabel(status)
  if (!window.confirm(`确认将「${row.canonical_question}」标记为${label}？`)) return
  try {
    const result = await updateFaqStatus(row.faq_id, status)
    const reflow = result?.reflow
    if (reflow && reflow.status === 'error') {
      faqFlash(`已标记为${label}，但知识库回流失败：${reflow.error}`, true)
    } else if (reflow) {
      faqFlash(
        reflow.cleared
          ? `已标记为${label}，回流文档已清空（当前离线无已发布条目）`
          : `已标记为${label}，${reflow.entries} 条已回流知识库（${reflow.children} 个分片）`
      )
    } else {
      faqFlash(`已标记为${label}`)
    }
    await loadFaq()
  } catch (err) {
    faqFlash(err.message, true)
  }
}

async function reflowFaqNow() {
  if (!window.confirm('把全部已发布条目重新回流到知识库？')) return
  faqReflowing.value = true
  try {
    const result = await reflowFaq()
    faqFlash(
      result.cleared
        ? '回流完成：当前没有已发布条目，回流文档已清空'
        : `回流完成：${result.entries} 条已回流知识库（${result.children} 个分片）`
    )
  } catch (err) {
    faqFlash(err.message, true)
  } finally {
    faqReflowing.value = false
  }
}

async function openFaqDetail(row) {
  faqDetailVisible.value = true
  faqDetailLoading.value = true
  faqDetailError.value = ''
  faqDetail.value = null
  try {
    faqDetail.value = await getFaqEntry(row.faq_id)
  } catch (err) {
    faqDetailError.value = err.message
  } finally {
    faqDetailLoading.value = false
  }
}

onMounted(async () => {
  await Promise.all([loadTables(), loadRag(), loadFaq()])
})
</script>

<template>
  <div class="admin-overlay">
    <div class="admin-shell">
      <header class="admin-header">
        <div class="header-text">
          <h2>后台管理</h2>
          <span class="header-sub">数据库管理 · RAG 知识库管理 · FAQ 沉淀库</span>
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
        <button class="tab" :class="{ active: tab === 'faq' }" @click="tab = 'faq'">
          FAQ 沉淀库
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
      <div v-else-if="tab === 'rag'" class="admin-body rag-body">
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

      <!-- FAQ 沉淀库 -->
      <div v-else class="admin-body faq-body">
        <div v-if="faqError" class="alert error">{{ faqError }}</div>
        <div v-else-if="faqNotice" class="alert success">{{ faqNotice }}</div>

        <div class="stat-cards">
          <div class="stat-card">
            <span class="stat-label">沉淀条目</span>
            <span class="stat-value">{{ faqStats.total }}</span>
          </div>
          <div class="stat-card">
            <span class="stat-label">待审核（草稿）</span>
            <span class="stat-value warn">{{ faqStats.pending_review }}</span>
          </div>
          <div class="stat-card">
            <span class="stat-label">已通过 / 已发布</span>
            <span class="stat-value">{{ faqStats.published }}</span>
          </div>
          <div class="stat-card">
            <span class="stat-label">覆盖分类</span>
            <span class="stat-value">{{ Object.keys(faqStats.by_category).length }}</span>
          </div>
        </div>

        <div class="faq-panel">
          <div class="pane-title">
            条目审核 ({{ faqTotal }})
            <button class="btn small" :disabled="faqLoading" @click="loadFaq">刷新</button>
          </div>
          <div class="action-row">
            <select v-model="faqFilters.status" class="input">
              <option value="">全部状态</option>
              <option value="draft">待审核</option>
              <option value="approved">已通过</option>
              <option value="published">已发布</option>
              <option value="archived">已归档</option>
            </select>
            <input
              v-model="faqFilters.category"
              class="input"
              type="text"
              placeholder="分类（咨询 / 办理 / 故障 / 规则）"
            />
            <input
              v-model="faqFilters.q"
              class="input search"
              type="text"
              placeholder="搜索标准问句 / 解决方案 / 关键词"
              @keyup.enter="loadFaq"
            />
            <button class="btn primary" :disabled="faqLoading" @click="loadFaq">查询</button>
            <button
              class="btn"
              @click="faqFilters.status = ''; faqFilters.category = ''; faqFilters.q = ''; loadFaq()"
            >重置</button>
            <button class="btn" :disabled="faqReflowing" @click="reflowFaqNow">
              {{ faqReflowing ? '回流中…' : '重跑回流' }}
            </button>
          </div>
          <p class="hint">
            条目由对话回合自动沉淀，默认「待审核」；点击「发布」会把全部已发布条目回流到知识库，
            之后客服提问即可通过 RAG 检索到（归档下架后自动从知识库移除）。
            「重跑回流」用于失败重试：已发布条目被后续回合补充内容时状态不变、不会自动回流，
            副本会滞后到下次改状态，也可点这里手动同步。
          </p>

          <div class="grid-wrap">
            <div v-if="faqLoading" class="placeholder">加载中...</div>
            <div v-else-if="!faqRows.length" class="placeholder">暂无沉淀条目</div>
            <table v-else class="grid">
              <thead>
                <tr>
                  <th class="num-col">编号</th>
                  <th>标准问句</th>
                  <th class="num-col">分类</th>
                  <th class="num-col">合并</th>
                  <th class="num-col">置信度</th>
                  <th class="num-col">状态</th>
                  <th class="op-col">操作</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="row in faqRows" :key="row.faq_id">
                  <td class="num-col">{{ row.faq_id }}</td>
                  <td :title="row.canonical_question" class="question-cell">
                    {{ row.canonical_question }}
                  </td>
                  <td class="num-col">{{ row.category || '-' }}</td>
                  <td class="num-col">{{ row.merge_count }}</td>
                  <td class="num-col">{{ row.confidence.toFixed(2) }}</td>
                  <td class="num-col">
                    <span class="status-tag" :class="row.status">{{ statusLabel(row.status) }}</span>
                  </td>
                  <td class="op-col">
                    <button class="link-btn" @click="openFaqDetail(row)">详情</button>
                    <button
                      v-if="row.status !== 'published'"
                      class="link-btn"
                      @click="changeFaqStatus(row, 'published')"
                    >发布</button>
                    <button
                      v-if="row.status === 'draft'"
                      class="link-btn"
                      @click="changeFaqStatus(row, 'approved')"
                    >通过</button>
                    <button
                      v-if="row.status !== 'archived'"
                      class="link-btn danger"
                      @click="changeFaqStatus(row, 'archived')"
                    >归档</button>
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

    <!-- FAQ 条目详情弹窗 -->
    <div v-if="faqDetailVisible" class="modal-overlay" @click.self="faqDetailVisible = false">
      <div class="preview-modal">
        <div class="modal-header">
          <div class="preview-title">
            <h3>FAQ 条目详情</h3>
            <span class="preview-source">
              {{ faqDetail ? `${faqDetail.faq_id} · ${statusLabel(faqDetail.status)}` : '加载中' }}
            </span>
          </div>
          <button class="close-btn" @click="faqDetailVisible = false">×</button>
        </div>

        <div class="preview-body">
          <div v-if="faqDetailLoading" class="placeholder">加载中...</div>
          <div v-else-if="faqDetailError" class="alert error">{{ faqDetailError }}</div>

          <template v-else-if="faqDetail">
            <div class="chunk-card">
              <div class="chunk-head">
                <span class="chunk-index">标准问句</span>
                <span class="chunk-meta">分类 {{ faqDetail.category || '-' }}</span>
                <span class="chunk-meta">置信度 {{ faqDetail.confidence.toFixed(2) }}</span>
                <span class="chunk-meta">合并 {{ faqDetail.merge_count }} 次</span>
              </div>
              <pre class="chunk-text">{{ faqDetail.canonical_question }}</pre>
            </div>

            <div class="chunk-card" v-if="faqDetail.question_variants.length">
              <div class="chunk-head"><span class="chunk-index">同义问法</span></div>
              <pre class="chunk-text">{{ faqDetail.question_variants.join('\n') }}</pre>
            </div>

            <div class="chunk-card">
              <div class="chunk-head"><span class="chunk-index">解决方案</span></div>
              <pre class="chunk-text">{{ faqDetail.solution || '(空)' }}</pre>
            </div>

            <div class="chunk-card" v-if="faqDetail.preconditions.length">
              <div class="chunk-head"><span class="chunk-index">前置条件</span></div>
              <pre class="chunk-text">{{ faqDetail.preconditions.join('\n') }}</pre>
            </div>

            <div class="chunk-card" v-if="faqDetail.related_skills.length">
              <div class="chunk-head"><span class="chunk-index">关联技能</span></div>
              <pre class="chunk-text">{{ faqDetail.related_skills.join('、') }}</pre>
            </div>

            <div class="chunk-card">
              <div class="chunk-head">
                <span class="chunk-index">溯源</span>
                <span class="chunk-meta">来源链路 {{ faqDetail.source_route || '-' }}</span>
              </div>
              <pre class="chunk-text">会话 {{ faqDetail.source_session_id || '-' }} · 第 {{ faqDetail.source_turn_index }} 轮
关键词 {{ faqDetail.keywords || '-' }}
来源 {{ faqDetail.sources.length }} 条 · 创建 {{ faqDetail.created_at }} · 更新 {{ faqDetail.updated_at }}</pre>
            </div>
          </template>
        </div>

        <div class="modal-footer">
          <span class="footer-hint">{{ faqDetail ? faqDetail.faq_id : '' }}</span>
          <div class="footer-actions">
            <button class="btn" @click="faqDetailVisible = false">关闭</button>
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

/* FAQ 沉淀库 */
.faq-body {
  flex-direction: column;
  overflow-y: auto;
}

.faq-panel {
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  background: var(--bg-secondary);
  border: 1px solid var(--border-color);
  border-radius: var(--radius);
  padding: 12px;
}

.faq-panel .action-row {
  flex-wrap: wrap;
}

.faq-panel .action-row .input {
  flex: 1;
  min-width: 140px;
}

.faq-panel .grid-wrap {
  margin-top: 8px;
  max-height: 460px;
}

.question-cell {
  max-width: 380px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-primary);
}

.stat-value.warn {
  color: var(--warning);
}

.status-tag {
  padding: 2px 7px;
  border-radius: 3px;
  font-size: 11px;
  font-family: inherit;
}

.status-tag.draft {
  background: rgba(240, 196, 105, 0.16);
  color: var(--warning);
}

.status-tag.approved {
  background: rgba(127, 214, 138, 0.14);
  color: var(--success);
}

.status-tag.published {
  background: var(--accent-dim);
  color: var(--text-primary);
}

.status-tag.archived {
  background: var(--bg-tertiary);
  color: var(--text-subtle);
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
