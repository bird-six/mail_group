<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { apiFetch, request, requestForm } from './api'
import DesktopTitleBar from './DesktopTitleBar.vue'
import { normalizeCopiedEmail, parseRecipients } from './recipients'

type SendStatus = 'pending' | 'sending' | 'success' | 'failed' | 'uncertain'
type TaskStatus = 'pending' | 'running' | 'completed' | 'interrupted'
type SectionKey = 'home' | 'mail-content' | 'target-mails' | 'sender-cluster' | 'data-panel' | 'help'

interface SenderConfig {
  id: string
  name: string
  email: string
  has_auth_code: boolean
  smtp_host: string
  smtp_port: number
  enabled: boolean
}

interface RecipientItem {
  email: string
  name: string
}

interface Assignment {
  recipient: string
  sender: string
  status: SendStatus
  message: string
  finished_at?: string | null
}

interface TaskSummary {
  task_id: string
  subject: string
  uncertain: number
  resumable: number
  status: TaskStatus
  total: number
  success: number
  failed: number
  pending: number
  progress: number
  created_at: string
  updated_at: string
  assignments: Assignment[]
}

interface SavedMail {
  content_type: 'plain' | 'html'
  id: number
  subject: string
  body: string
  created_at: string
  attachments: string[]
}

interface StoredSender {
  id: number
  email: string
  has_auth_code: boolean
  smtp_host: string
  smtp_port: number
  note: string
  enabled: boolean
  created_at: string
}

interface StoredRecipient {
  id: number
  email: string
  enabled: boolean
  note: string
  created_at: string
}

interface StatItem {
  label: string
  total: number
  success: number
  failed: number
}

interface StatsResponse {
  stats: StatItem[]
  total_success: number
  total_failed: number
  success_rate: number
  total_prev: number
  success_prev: number
  failed_prev: number
  rate_prev: number
  period_rate: number
  period_success: number
  period_failed: number
}

interface SenderBreakdown {
  sender: string
  total: number
  success: number
  failed: number
  success_rate: number
}

interface RecipientBreakdown {
  recipient: string
  note: string
  total: number
  success: number
  failed: number
}

interface FailedRecipient {
  recipient: string
  sender: string
  message: string
  finished_at: string
}

interface FailedReason {
  reason: string
  count: number
  percentage: number
}

// 失败原因中文映射
const ERROR_CODE_MAP: Record<string, string> = {
  '550 Mailbox not found': '邮箱不存在',
  '550 User doesn\'t exist': '收件用户不存在',
  '550 Requested action not taken: mailbox unavailable': '邮箱不可用',
  '552 Message size exceeds fixed limit': '邮件大小超出限制',
  '554 Transaction failed': '邮件被拒收',
  '550 Invalid recipient': '无效收件人',
  'Connection timeout': '连接超时',
  'Connection refused': '连接被拒绝',
  'Authentication failed': '认证失败（请检查授权码）',
  '421 Service not available': '服务暂不可用',
  '450 Requested mail action not taken: mailbox unavailable': '邮箱暂时不可用',
  '451 Requested action aborted: local error in processing': '本地处理错误',
  '452 Requested action not taken: insufficient system storage': '存储空间不足',
  '500 Syntax error, command unrecognized': '命令语法错误',
  '501 Syntax error in parameters or arguments': '参数语法错误',
  '502 Command not implemented': '命令未实现',
  '503 Bad sequence of commands': '命令顺序错误',
  'SMTP error': 'SMTP 协议错误',
  'DNS error': 'DNS 解析失败',
  'TLS error': '加密连接失败',
  'Too many connections': '连接数过多',
  'Rate limit exceeded': '发送频率超限',
  'Network error': '网络错误',
  'Timeout': '超时',
}

function translateErrorMessage(msg: string | null | undefined): string {
  if (!msg) return '-'
  const trimmed = msg.trim()
  if (ERROR_CODE_MAP[trimmed]) return ERROR_CODE_MAP[trimmed]
  // 模糊匹配：检测是否包含关键错误码
  for (const key of Object.keys(ERROR_CODE_MAP)) {
    if (trimmed.includes(key)) return ERROR_CODE_MAP[key]!
  }
  const codeMatch = trimmed.match(/^(4\d\d|5\d\d)/)
  if (codeMatch) {
    const code: string = codeMatch[1]!
    if (code.startsWith('5')) return `发送被拒绝 (${code})`
    if (code.startsWith('4')) return `临时失败 (${code})`
  }
  return trimmed
}

type StatsPeriod = 'hour' | 'day' | 'week' | 'month'

const isDesktop = Boolean(window.desktop)
const templateLoading = ref(false)
const templateError = ref('')
let templateVersion = 0
let templateAbort: AbortController | undefined
let submission: { signature: string; id: string } | null = null
let polling = false

const content = reactive({
  content_type: 'plain' as 'plain' | 'html',
  subject: '',
  body: '',
})

const attachments = ref<File[]>([])
const attachmentError = ref('')
const maxAttachmentSize = 50 * 1024 * 1024
const blockedAttachmentExtensions = ['.exe', '.bat', '.cmd', '.com', '.scr', '.js', '.vbs', '.msi', '.jar']

const emailRegex = /^[^\s@]+@[^\s@]+\.[^\s@]+$/

function isValidEmail(email: string): boolean {
  return emailRegex.test(email.trim().toLowerCase())
}

const savedSenders = ref<StoredSender[]>([])
const storedRecipients = ref<StoredRecipient[]>([])
const recipientsText = ref('')
const recipientSearch = ref('')
const recipientError = ref('')
const recipientImportMessage = ref('')
const parsingRecipients = ref(false)
const recipientInputDirty = ref(false)
const recipientPage = ref(1)
const recipientPageSize = 8
const taskSearch = ref('')
const taskPage = ref(1)
const taskPageSize = 8
const pendingExpanded = ref<Record<string, boolean>>({})

function togglePendingList(taskId: string) {
  pendingExpanded.value = { ...pendingExpanded.value, [taskId]: !pendingExpanded.value[taskId] }
}

function isPendingExpanded(taskId: string): boolean {
  return !!pendingExpanded.value[taskId]
}

const tasks = ref<TaskSummary[]>([])
const currentTaskId = ref('')
const showTaskDetail = ref(false)
const selectedTask = computed(() => tasks.value.find(task => task.task_id === currentTaskId.value))
const savedMails = ref<SavedMail[]>([])
const selectedSavedMailId = ref<number | null>(null)
const loading = ref(false)
const errorMessage = ref('')
const activeSection = ref<SectionKey>('home')
const showSenderModal = ref(false)
const editingSenderId = ref<number | null>(null)
const showDetailModal = ref(false)
const detailMail = ref<SavedMail | null>(null)
const showRecipientModal = ref(false)
const modalRecipientEmail = ref('')
const modalRecipientNote = ref('')
const modalRecipientError = ref('')
const showEditRecipientModal = ref(false)
const editingRecipient = ref<StoredRecipient | null>(null)
const editRecipientEmail = ref('')
const editRecipientNote = ref('')
const showHelpTooltip = ref(false)
const showRecipientStatusHelp = ref(false)
const senderForm = reactive({
  email: '',
  auth_code: '',
  smtp_host: 'smtp.qq.com',
  smtp_port: 465,
  note: '',
})
const selectedPreset = ref('')
const emailPresets = [
  { type: 'qq', label: 'QQ 邮箱', host: 'smtp.qq.com', port: 465 },
  { type: '126', label: '126 邮箱', host: 'smtp.126.com', port: 465 },
  { type: '163', label: '163 邮箱', host: 'smtp.163.com', port: 465 },
  { type: 'gmail', label: 'Gmail', host: 'smtp.gmail.com', port: 587 },
  { type: 'outlook', label: 'Outlook', host: 'smtp-mail.outlook.com', port: 587 },
  { type: 'other', label: '其它邮箱', host: '', port: 465 },
]

function applyPreset() {
  const preset = emailPresets.find((p) => p.type === selectedPreset.value)
  if (preset) {
    senderForm.smtp_host = preset.host
    senderForm.smtp_port = preset.port
  }
}
const statsData = ref<StatsResponse | null>(null)
const statsPeriod = ref<StatsPeriod>('hour')
const senderBreakdown = ref<SenderBreakdown[]>([])
const recipientBreakdown = ref<RecipientBreakdown[]>([])
const failedRecipients = ref<FailedRecipient[]>([])
const failedRecipientsTotal = ref(0)
const failedRecipientPage = ref(1)
const failedRecipientPageSize = ref(10)
const failedRecipientJump = ref('')
const failureReasons = ref<FailedReason[]>([])
let timer: number | undefined

const pieHover = ref<{ x: number; y: number; index: number } | null>(null)

function handlePieMouseMove(e: MouseEvent) {
  const el = e.currentTarget as HTMLElement
  const rect = el.getBoundingClientRect()
  const cx = rect.width / 2
  const cy = rect.height / 2
  const mx = e.clientX - rect.left - cx
  const my = e.clientY - rect.top - cy
  if (Math.sqrt(mx * mx + my * my) < 45) {
    pieHover.value = null
    return
  }
  let angle = Math.atan2(my, mx) * (180 / Math.PI) + 90
  if (angle < 0) angle += 360
  const pct = (angle / 360) * 100
  let acc = 0
  const items = failureReasons.value
  for (let i = 0; i < items.length; i++) {
    const item = items[i]!
    acc += item.percentage
    if (pct <= acc) {
      pieHover.value = { x: e.clientX - rect.left, y: e.clientY - rect.top, index: i }
      return
    }
  }
  pieHover.value = null
}

function handlePieMouseLeave() {
  pieHover.value = null
}

const senders = computed<SenderConfig[]>(() =>
  savedSenders.value.map((s) => ({
    id: String(s.id),
    name: s.note,
    email: s.email,
    has_auth_code: s.has_auth_code,
    smtp_host: s.smtp_host,
    smtp_port: s.smtp_port,
    enabled: s.enabled,
  })),
)

const enabledSenders = computed(() => senders.value.filter((s) => s.enabled && s.email.trim()))

const recipients = computed<RecipientItem[]>(() =>
  storedRecipients.value.map((r) => ({ email: r.email, name: '' })),
)
const enabledRecipients = computed<RecipientItem[]>(() =>
  storedRecipients.value.filter((r) => r.enabled).map((r) => ({ email: r.email, name: '' })),
)
const filteredRecipients = computed(() => {
  const keyword = recipientSearch.value.trim().toLowerCase()
  if (!keyword) return storedRecipients.value
  return storedRecipients.value.filter((r) => r.email.includes(keyword))
})
const recipientTotalPages = computed(() => Math.max(1, Math.ceil(filteredRecipients.value.length / recipientPageSize)))
const pagedRecipients = computed(() => {
  const safePage = Math.min(recipientPage.value, recipientTotalPages.value)
  const start = (safePage - 1) * recipientPageSize
  return filteredRecipients.value.slice(start, start + recipientPageSize)
})
const filteredTasks = computed(() => {
  const keyword = taskSearch.value.trim().toLowerCase()
  if (!keyword) return tasks.value
  return tasks.value.filter((task) => {
    const taskIdMatched = task.task_id.toLowerCase().includes(keyword)
    const recipientMatched = task.assignments.some((item) => item.recipient.toLowerCase().includes(keyword))
    const senderMatched = task.assignments.some((item) => item.sender.toLowerCase().includes(keyword))
    return taskIdMatched || recipientMatched || senderMatched
  })
})
const taskTotalPages = computed(() => Math.max(1, Math.ceil(filteredTasks.value.length / taskPageSize)))
const pagedTasks = computed(() => {
  const safePage = Math.min(taskPage.value, taskTotalPages.value)
  const start = (safePage - 1) * taskPageSize
  return filteredTasks.value.slice(start, start + taskPageSize)
})
const previewAssignments = computed(() => {
  const activeSenders = enabledSenders.value
  if (!activeSenders.length || !enabledRecipients.value.length) return []
  return enabledRecipients.value.map((recipient, index) => {
    const sender = activeSenders[index % activeSenders.length]
    return {
      recipient: recipient.email,
      sender: sender?.email ?? '',
    }
  })
})
const dashboard = computed(() => {
  const totalTasks = tasks.value.length
  const totalMails = tasks.value.reduce((sum, task) => sum + task.total, 0)
  const running = tasks.value.filter((task) => ['pending', 'running'].includes(task.status)).length
  return { totalTasks, totalMails, running }
})
const runningTasks = computed(() => tasks.value.filter((task) => ['pending', 'running'].includes(task.status)))
const totalSuccess = computed(() => statsData.value?.period_success ?? 0)
const totalFailed = computed(() => statsData.value?.period_failed ?? 0)
const totalAll = computed(() => totalSuccess.value + totalFailed.value)
const overallRate = computed(() => statsData.value?.period_rate ?? 0)
const trendArrow = computed(() => {
  if (!statsData.value || statsData.value.total_prev === 0) return null
  const diff = statsData.value.period_rate - statsData.value.rate_prev
  return { up: diff >= 0, value: Math.abs(diff).toFixed(1) }
})
const canSend = computed(() => Boolean(content.subject.trim() && content.body.trim() && enabledSenders.value.length && enabledRecipients.value.length && !parsingRecipients.value && !recipientInputDirty.value && !templateLoading.value && !templateError.value && !loading.value))

function openAddSenderModal() {
  editingSenderId.value = null
  senderForm.email = ''
  senderForm.auth_code = ''
  senderForm.smtp_host = 'smtp.qq.com'
  senderForm.smtp_port = 465
  senderForm.note = ''
  selectedPreset.value = ''
  showSenderModal.value = true
}

function openEditSenderModal(sender: StoredSender) {
  editingSenderId.value = sender.id
  senderForm.email = sender.email
  senderForm.auth_code = ''
  senderForm.smtp_host = sender.smtp_host
  senderForm.smtp_port = sender.smtp_port
  senderForm.note = sender.note
  selectedPreset.value = ''
  showSenderModal.value = true
}

async function submitSenderForm() {
  if (!senderForm.email.trim() || (!editingSenderId.value && !senderForm.auth_code.trim())) return
  try {
    if (editingSenderId.value) {
      await request<StoredSender>(`/api/sender-configs/${editingSenderId.value}`, {
        method: 'PUT',
        body: JSON.stringify(senderForm),
      })
    } else {
      await request<StoredSender>('/api/sender-configs', {
        method: 'POST',
        body: JSON.stringify(senderForm),
      })
    }
    showSenderModal.value = false
    await loadSenders()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '保存发送邮箱失败'
  }
}

async function removeSender(id: string) {
  try {
    await request(`/api/sender-configs/${id}`, { method: 'DELETE' })
    await loadSenders()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '删除发送邮箱失败'
  }
}

async function toggleSender(sender: StoredSender) {
  try {
    const result = await request<{ enabled: boolean }>(`/api/sender-configs/${sender.id}/toggle`, { method: 'PATCH' })
    sender.enabled = result.enabled
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '切换状态失败'
  }
}

function syncRecipients(nextRecipients: RecipientItem[]) {
  recipientsText.value = nextRecipients.map((recipient) => recipient.email).join('\n')
  if (recipientPage.value > recipientTotalPages.value) {
    recipientPage.value = recipientTotalPages.value
  }
}

function onRecipientsInput() {
  recipientPage.value = 1
  recipientInputDirty.value = true
  recipientImportMessage.value = ''
  recipientError.value = ''
}

async function parseRecipientsText() {
  if (parsingRecipients.value) return
  recipientError.value = ''
  recipientImportMessage.value = ''
  errorMessage.value = ''
  const items = parseRecipients(recipientsText.value)
  if (!items.length) {
    recipientError.value = '请输入有效的邮箱地址'
    return
  }
  parsingRecipients.value = true
  try {
    await saveRecipientsToDb(items)
    recipientsText.value = storedRecipients.value.map((r) => r.email).join('\n')
    recipientPage.value = 1
    recipientInputDirty.value = false
    recipientImportMessage.value = `已保存 ${storedRecipients.value.length} 个邮箱，复制格式已清理，重复地址已合并`
  } catch (error) {
    recipientError.value = error instanceof Error ? error.message : '保存收件人失败'
  } finally {
    parsingRecipients.value = false
  }
}

function openAddRecipientModal() {
  modalRecipientEmail.value = ''
  modalRecipientNote.value = ''
  modalRecipientError.value = ''
  showRecipientModal.value = true
}

async function confirmAddRecipient() {
  modalRecipientError.value = ''
  const email = normalizeCopiedEmail(modalRecipientEmail.value)
  if (!email) return
  if (!isValidEmail(email)) {
    modalRecipientError.value = `邮箱格式无效：${email}`
    return
  }
  try {
    const created = await request<StoredRecipient>('/api/recipients', {
      method: 'POST',
      body: JSON.stringify({ email, note: modalRecipientNote.value.trim() }),
    })
    if (!storedRecipients.value.find((r) => r.email === created.email)) {
      storedRecipients.value.push(created)
    } else {
      const existing = storedRecipients.value.find((r) => r.email === created.email)
      if (existing && modalRecipientNote.value.trim()) {
        existing.note = created.note
      }
    }
    recipientsText.value = storedRecipients.value.map((r) => r.email).join('\n')
    syncRecipients(parseRecipients(recipientsText.value))
    modalRecipientEmail.value = ''
    modalRecipientNote.value = ''
    showRecipientModal.value = false
    recipientPage.value = recipientTotalPages.value
  } catch (error) {
    modalRecipientError.value = error instanceof Error ? error.message : '添加收件人失败'
  }
}

async function loadRecipientsFromDb(syncInput = true) {
  try {
    storedRecipients.value = await request<StoredRecipient[]>('/api/recipients')
    if (syncInput && !recipientInputDirty.value) recipientsText.value = storedRecipients.value.map((r) => r.email).join('\n')
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '获取收件人失败'
  }
}

async function saveRecipientsToDb(items: RecipientItem[]) {
  await request<StoredRecipient[]>('/api/recipients/batch', {
    method: 'POST',
    body: JSON.stringify(items.map((r) => ({ email: r.email }))),
  })
  storedRecipients.value = await request<StoredRecipient[]>('/api/recipients')
}

async function removeRecipient(email: string) {
  const target = storedRecipients.value.find((r) => r.email === email)
  if (target) {
    try {
      await request(`/api/recipients/${target.id}`, { method: 'DELETE' })
      storedRecipients.value = storedRecipients.value.filter((r) => r.id !== target.id)
    } catch (error) {
      errorMessage.value = error instanceof Error ? error.message : '删除收件人失败'
    }
  }
  const nextRecipients = recipients.value.filter((recipient) => recipient.email !== email)
  syncRecipients(nextRecipients)
}

async function toggleRecipient(id: number) {
  try {
    const result = await request<StoredRecipient>(`/api/recipients/${id}/toggle`, { method: 'PATCH' })
    const item = storedRecipients.value.find((r) => r.id === id)
    if (item) {
      item.enabled = result.enabled
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '切换状态失败'
  }
}

async function batchToggleRecipients(enabled: boolean) {
  try {
    await request('/api/recipients/batch-toggle', {
      method: 'POST',
      body: JSON.stringify({ enabled }),
    })
    storedRecipients.value.forEach((r) => { r.enabled = enabled })
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '批量操作失败'
  }
}

function openEditRecipientModal(recipient: StoredRecipient) {
  editingRecipient.value = recipient
  editRecipientEmail.value = recipient.email
  editRecipientNote.value = recipient.note
  showEditRecipientModal.value = true
}

async function confirmEditRecipient() {
  if (!editingRecipient.value) return
  const email = normalizeCopiedEmail(editRecipientEmail.value)
  if (!email || !isValidEmail(email)) {
    errorMessage.value = `邮箱格式无效：${email}`
    return
  }
  try {
    const updated = await request<StoredRecipient>(`/api/recipients/${editingRecipient.value.id}`, {
      method: 'PATCH',
      body: JSON.stringify({ email, note: editRecipientNote.value }),
    })
    const item = storedRecipients.value.find((r) => r.id === updated.id)
    if (item) {
      item.email = updated.email
      item.note = updated.note
    }
    recipientsText.value = storedRecipients.value.map((r) => r.email).join('\n')
    showEditRecipientModal.value = false
    editingRecipient.value = null
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '编辑收件人失败'
  }
}

function buildPayloadFormData() {
  const formData = new FormData()
  formData.append(
    'payload',
    JSON.stringify({
      content: {
        subject: content.subject,
        body: content.body,
        content_type: content.content_type,
      },
      sender_ids: enabledSenders.value.map(sender => Number(sender.id)),
      request_id: submission?.id,
      recipients: enabledRecipients.value,
    }),
  )
  attachments.value.forEach((file) => {
    formData.append('attachments', file)
  })
  return formData
}

function changeRecipientPage(page: number) {
  recipientPage.value = Math.min(Math.max(page, 1), recipientTotalPages.value)
}

function changeTaskPage(page: number) {
  taskPage.value = Math.min(Math.max(page, 1), taskTotalPages.value)
}

function formatFileSize(size: number) {
  if (size >= 1024 * 1024) return `${(size / 1024 / 1024).toFixed(2)} MB`
  return `${(size / 1024).toFixed(1)} KB`
}

function validateAttachment(file: File) {
  const lowerName = file.name.toLowerCase()
  const blocked = blockedAttachmentExtensions.some((extension) => lowerName.endsWith(extension))
  if (blocked) return 'QQ 邮箱不建议发送可执行脚本类附件'
  if (file.size > maxAttachmentSize) return '单个附件不能超过 50MB'
  return ''
}

function appendAttachments(fileList: FileList | File[]) {
  if (templateLoading.value) return
  attachmentError.value = ''
  const nextFiles = [...attachments.value]
  Array.from(fileList).forEach((file) => {
    const error = validateAttachment(file)
    if (error) {
      attachmentError.value = `${file.name}：${error}`
      return
    }
    const duplicated = nextFiles.some((item) => item.name === file.name && item.size === file.size)
    if (!duplicated) nextFiles.push(file)
  })
  attachments.value = nextFiles
}

function handleAttachmentChange(event: Event) {
  const target = event.target as HTMLInputElement
  if (target.files) appendAttachments(target.files)
  target.value = ''
}

function handleAttachmentDrop(event: DragEvent) {
  const files = event.dataTransfer?.files
  if (files) appendAttachments(files)
}

function removeAttachment(index: number) {
  attachments.value = attachments.value.filter((_, fileIndex) => fileIndex !== index)
}

async function loadTasks() {
  try {
    tasks.value = await request<TaskSummary[]>('/api/tasks')
    const firstTask = tasks.value.at(0)
    if (!currentTaskId.value && firstTask) currentTaskId.value = firstTask.task_id
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '获取任务失败'
  }
}

async function loadSenders() {
  try {
    savedSenders.value = await request<StoredSender[]>('/api/sender-configs')
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '获取发送邮箱失败'
  }
}

async function loadSavedMails() {
  try {
    savedMails.value = await request<SavedMail[]>('/api/saved-mails')
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '获取保存的邮件失败'
  }
}

async function loadStats() {
  try {
    statsData.value = await request<StatsResponse>(`/api/stats?period=${statsPeriod.value}`)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '获取统计数据失败'
  }
}

async function loadSenderBreakdown() {
  try {
    senderBreakdown.value = await request<SenderBreakdown[]>('/api/stats/sender-breakdown')
  } catch { /* 静默失败 */ }
}

async function loadRecipientBreakdown() {
  try {
    recipientBreakdown.value = await request<RecipientBreakdown[]>('/api/stats/recipient-breakdown')
  } catch { /* 静默失败 */ }
}

async function loadFailedRecipients() {
  try {
    const data = await request<FailedRecipient[]>(`/api/stats/failed-recipients?page=${failedRecipientPage.value}&page_size=${failedRecipientPageSize.value}`)
    failedRecipients.value = data
    const totalData = await request<{ total: number }>('/api/stats/failed-recipients/total')
    failedRecipientsTotal.value = totalData.total
  } catch { /* 静默失败 */ }
}

async function exportFailedRecipients() {
  try {
    const emails = await request<string[]>('/api/stats/failed-recipients/emails')
    if (!emails.length) {
      errorMessage.value = '暂无失败邮箱可导出'
      return
    }
    const blob = new Blob([emails.join('\n')], { type: 'text/plain' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `failed_recipients_${new Date().toISOString().slice(0, 10)}.txt`
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

async function loadFailureReasons() {
  try {
    failureReasons.value = await request<FailedReason[]>('/api/stats/failure-reasons')
  } catch { /* 静默失败 */ }
}

async function loadAllStats() {
  await Promise.all([
    loadStats(),
    loadSenderBreakdown(),
    loadRecipientBreakdown(),
    loadFailedRecipients(),
    loadFailureReasons(),
  ])
}

function changeFailedRecipientPage(page: number) {
  failedRecipientPage.value = page
  void loadFailedRecipients()
}

function jumpFailedRecipientPage() {
  const n = parseInt(failedRecipientJump.value, 10)
  if (isNaN(n) || n < 1 || n > failedRecipientTotalPages.value) return
  changeFailedRecipientPage(n)
  failedRecipientJump.value = ''
}

const failedRecipientTotalPages = computed(() =>
  Math.max(1, Math.ceil(failedRecipientsTotal.value / failedRecipientPageSize.value))
)

interface PageItem {
  label: string
  value: number | null
}
const failedRecipientPages = computed<PageItem[]>(() => {
  const total = failedRecipientTotalPages.value
  const cur = failedRecipientPage.value
  if (total <= 10) {
    return Array.from({ length: total }, (_, i) => ({ label: String(i + 1), value: i + 1 }))
  }
  const pages: PageItem[] = [{ label: '1', value: 1 }, { label: '2', value: 2 }, { label: '3', value: 3 }]
  if (cur <= 5) {
    for (let i = 4; i <= 6; i++) pages.push({ label: String(i), value: i })
    pages.push({ label: '...', value: null })
  } else if (cur >= total - 4) {
    pages.push({ label: '...', value: null })
    for (let i = total - 5; i <= total - 3; i++) pages.push({ label: String(i), value: i })
  } else {
    pages.push({ label: '...', value: null })
    for (let i = cur - 1; i <= cur + 1; i++) pages.push({ label: String(i), value: i })
    pages.push({ label: '...', value: null })
  }
  for (let i = total - 2; i <= total; i++) pages.push({ label: String(i), value: i })
  return pages
})

function isPageActive(p: PageItem): boolean {
  return p.value !== null && p.value === failedRecipientPage.value
}

const failedRecipientRange = computed(() => {
  const start = (failedRecipientPage.value - 1) * failedRecipientPageSize.value + 1
  const end = Math.min(failedRecipientPage.value * failedRecipientPageSize.value, failedRecipientsTotal.value)
  return `${start}-${end}`
})

// 饼图颜色池
const PIE_COLORS = [
  '#ef4444', '#f97316', '#eab308', '#22c55e', '#14b8a6',
  '#3b82f6', '#8b5cf6', '#ec4899', '#64748b', '#06b6d4',
]

function pieColor(index: number): string {
  return PIE_COLORS[index % PIE_COLORS.length]!
}

function pieChartStyle(): string {
  if (!failureReasons.value.length) return ''
  let acc = 0
  const segments: string[] = []
  failureReasons.value.forEach((r, idx) => {
    const color = pieColor(idx)
    const start = acc
    acc += r.percentage
    const end = acc
    segments.push(`${color} ${start}% ${end}%`)
  })
  // 如果不足100%，补灰色
  if (acc < 100) {
    segments.push(`#e2e8f0 ${acc}% 100%`)
  }
  return `conic-gradient(${segments.join(', ')})`
}

interface PieSliceSpec {
  start: number
  end: number
  color: string
  translate: string
  rotate: string
  clipPoints: string
}

function pieSliceList(): PieSliceSpec[] {
  if (!failureReasons.value.length) return []
  let acc = 0
  return failureReasons.value.map((r, idx) => {
    const start = acc
    acc += r.percentage
    const end = acc
    const color = pieColor(idx)
    // 计算扇形中心点的方向（度数，从 12 点方向顺时针）
    const centerDeg = ((start + end) / 2) * 3.6
    const rad = (centerDeg - 90) * (Math.PI / 180)
    const dx = Math.round(Math.cos(rad) * 5)
    const dy = Math.round(Math.sin(rad) * 5)
    const translate = `translate(${dx}px, ${dy}px)`
    // 小幅度旋转：沿扇形中心方向微旋
    const rotateDir = centerDeg < 180 ? 1 : -1
    const rotate = `rotate(${rotateDir * 1.5}deg)`
    // 生成 clip-path 扇形的多边形点（围绕圆周采样）
    const startDeg = start * 3.6
    const endDeg = end * 3.6
    const arcDeg = endDeg - startDeg
    const steps = Math.max(2, Math.ceil(arcDeg / 8))
    const points: string[] = ['50% 50%']
    for (let i = 0; i <= steps; i++) {
      const deg = startDeg + (arcDeg * i) / steps
      const r = (deg - 90) * (Math.PI / 180)
      const px = 50 + Math.cos(r) * 50
      const py = 50 + Math.sin(r) * 50
      points.push(`${px.toFixed(3)}% ${py.toFixed(3)}%`)
    }
    points.push('50% 50%')
    return { start, end, color, translate, rotate, clipPoints: points.join(', ') }
  })
}

function dotLeftPct(index: number, total: number): number {
  if (total <= 1) return 50
  return ((index + 0.5) / total) * 100
}

function rateLinePath(items: StatItem[]): string {
  if (!items.length) return ''
  if (items.length === 1) {
    const first = items[0]!
    const y = first.total ? 100 - Math.max((first.success / first.total) * 100, 4) : 100
    return `M 50 ${y}`
  }

  const n = items.length
  const points = items.map((item, i) => {
    const x = ((i + 0.5) / n) * 100
    const y = item.total ? 100 - Math.max((item.success / item.total) * 100, 4) : 100
    return { x, y }
  })

  const first = points[0]!
  const path: string[] = [`M ${first.x} ${first.y}`]
  for (let i = 0; i < points.length - 1; i++) {
    const current = points[i]!
    const next = points[i + 1]!
    const controlX = (current.x + next.x) / 2
    path.push(`C ${controlX} ${current.y}, ${controlX} ${next.y}, ${next.x} ${next.y}`)
  }
  return path.join(' ')
}

function switchStatsPeriod(period: StatsPeriod) {
  statsPeriod.value = period
  void loadStats()
}

async function saveCurrentMail() {
  if (templateLoading.value || templateError.value) return
  const subject = content.subject.trim()
  const body = content.body.trim()
  if (!subject || !body) {
    errorMessage.value = '主题和正文不能为空'
    return
  }
  try {
    const formData = new FormData()
    formData.append('subject', subject)
    formData.append('body', body)
    formData.append('content_type', content.content_type)
    attachments.value.forEach((file) => {
      formData.append('attachments', file)
    })
    const saved = await requestForm<SavedMail>('/api/saved-mails', formData)
    savedMails.value.unshift(saved)
    errorMessage.value = ''
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '保存邮件失败'
  }
}

function formatDate(iso: string) {
  return iso.replace('T', ' ')
}

function getAttachmentUrl(mail: SavedMail, filename: string) {
  return `/api/saved-mails/${mail.id}/attachments/${encodeURIComponent(filename)}`
}

function barHeight(value: number, maxValue: number): string {
  const max = Math.max(maxValue, 1)
  return `${Math.max((value / max) * 100, value > 0 ? 4 : 0)}%`
}

function chartMax(items: StatItem[], field: 'total' | 'success' | 'failed'): number {
  return Math.max(...items.map((item) => item[field]), 0)
}

function getYAxisTicks(maxVal: number): number[] {
  if (maxVal <= 0) return [0, 0, 0, 0, 0]
  return [
    0,
    Math.round(maxVal * 0.25),
    Math.round(maxVal * 0.5),
    Math.round(maxVal * 0.75),
    maxVal,
  ]
}

function buildGridItems(period: StatsPeriod): StatItem[] {
  const now = new Date()
  const y = now.getFullYear()
  const m = now.getMonth() + 1
  const mon = String(m).padStart(2, '0')
  const day = String(now.getDate()).padStart(2, '0')

  if (period === 'hour') {
    return Array.from({ length: 24 }, (_, i) => ({
      label: `${y}-${mon}-${day} ${String(i).padStart(2, '0')}:00`,
      total: 0,
      success: 0,
      failed: 0,
    }))
  }
  if (period === 'day') {
    const daysInMonth = new Date(y, m, 0).getDate()
    return Array.from({ length: daysInMonth }, (_, i) => ({
      label: `${y}-${mon}-${String(i + 1).padStart(2, '0')}`,
      total: 0,
      success: 0,
      failed: 0,
    }))
  }
  if (period === 'week') {
    return Array.from({ length: 5 }, (_, i) => ({
      label: `${y}-${mon}-${i + 1}`,
      total: 0,
      success: 0,
      failed: 0,
    }))
  }
  return Array.from({ length: 12 }, (_, i) => ({
    label: `${y}-${String(i + 1).padStart(2, '0')}`,
    total: 0,
    success: 0,
    failed: 0,
  }))
}

function fillGridData(grid: StatItem[], data: StatItem[]): StatItem[] {
  const byLabel = new Map(data.map(item => [item.label, item]))
  return grid.map(item => byLabel.get(item.label) ?? item)
}

function chartLabel(label: string, period: StatsPeriod): string {
  if (period === 'hour') return label.split(' ')[1] || label
  if (period === 'day') return label.split('-')[2] || label
  if (period === 'week') {
    const w = label.split('-')[2]
    return w ? `第${w}周` : label
  }
  return label.split('-')[1] || label
}

function formatTooltipLabel(label: string, period: StatsPeriod): string {
  if (period === 'hour') {
    const parts = label.split(' ')
    return `${parts[0]} ${parts[1] || ''}`
  }
  if (period === 'day') return label
  if (period === 'week') {
    const parts = label.split('-')
    return `${parts[0]}-${parts[1]} 第${parts[2]}周`
  }
  return label
}

const successRateClass = computed(() => {
  const rate = overallRate.value
  if (rate >= 90) return 'rate-high'
  if (rate >= 70) return 'rate-mid'
  return 'rate-low'
})

const gridItems = computed(() => {
  if (!statsData.value) return null
  const grid = buildGridItems(statsPeriod.value)
  return fillGridData(grid, statsData.value.stats)
})

async function selectSavedMail(mail: SavedMail) {
  const version = ++templateVersion
  templateAbort?.abort()
  templateAbort = new AbortController()
  const signal = templateAbort.signal
  selectedSavedMailId.value = mail.id
  content.subject = mail.subject
  content.body = mail.body
  content.content_type = mail.content_type ?? 'plain'
  attachments.value = []
  templateError.value = ''
  templateLoading.value = true
  try {
    const files = await Promise.all(mail.attachments.map(async name => {
      const response = await apiFetch(getAttachmentUrl(mail, name), { signal })
      const blob = await response.blob()
      return new File([blob], name.split('_').slice(1).join('_') || name, { type: blob.type })
    }))
    if (version === templateVersion) attachments.value = files
  } catch (error) {
    if (version === templateVersion) templateError.value = `模板附件未完整加载，已阻止发送。${error instanceof Error ? error.message : '请重新选择模板'}`
  } finally {
    if (version === templateVersion) templateLoading.value = false
  }
}

async function downloadAttachment(mail: SavedMail, name: string) {
  try {
    const blob = await (await apiFetch(getAttachmentUrl(mail, name))).blob()
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = name.split('_').slice(1).join('_') || name
    link.click()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '附件下载失败'
  }
}

async function resumeTask(task: TaskSummary, retryUncertain = false) {
  if (retryUncertain && !confirm('这些邮件上次发送时连接中断，可能已经送达。请先在发件箱核对，确认需要重新发送后继续。')) return
  try {
    await request(`/api/tasks/${task.task_id}/resume`, { method: 'POST', body: JSON.stringify({ retry_uncertain: retryUncertain }) })
    await loadTasks()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '恢复失败'
  }
}

function viewMailDetail(mail: SavedMail) {
  detailMail.value = mail
  showDetailModal.value = true
}

async function confirmDeleteSavedMail(id: number) {
  if (!confirm('确定要删除此邮件模板吗？')) return
  try {
    await request(`/api/saved-mails/${id}`, { method: 'DELETE' })
    savedMails.value = savedMails.value.filter((mail) => mail.id !== id)
    if (selectedSavedMailId.value === id) {
      selectedSavedMailId.value = null
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '删除保存的邮件失败'
  }
}

async function submitTask() {
  if (!canSend.value) return
  const signature = JSON.stringify([content, enabledSenders.value.map(s => s.id), enabledRecipients.value,
    attachments.value.map(f => [f.name, f.size, f.lastModified])])
  if (!submission || submission.signature !== signature) submission = { signature, id: crypto.randomUUID() }
  loading.value = true
  errorMessage.value = ''
  try {
    const task = await requestForm<TaskSummary>('/api/tasks', buildPayloadFormData())
    currentTaskId.value = task.task_id
    submission = null
    await Promise.all([loadTasks(), loadRecipientsFromDb(false)])
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '创建任务失败'
  } finally {
    loading.value = false
  }
}

function statusText(status: TaskStatus | SendStatus) {
  const map: Record<TaskStatus | SendStatus, string> = {
    pending: '等待中',
    running: '发送中',
    completed: '已完成',
    sending: '发送中',
    success: '成功',
    failed: '失败',
    interrupted: '已中断',
    uncertain: '待核对',
  }
  return map[status]
}

function switchSection(sectionId: SectionKey) {
  activeSection.value = sectionId
  if (showSenderModal.value) showSenderModal.value = false
}

function goToHelp() {
  showSenderModal.value = false
  activeSection.value = 'help'
}

onMounted(() => {
  void loadTasks()
  void loadSenders()
  void loadRecipientsFromDb()
  void loadSavedMails()
  void loadAllStats()
  timer = window.setInterval(async () => {
    if (polling) return
    polling = true
    try { await Promise.all([loadTasks(), loadAllStats(), loadRecipientsFromDb(false)]) }
    finally { polling = false }
  }, 3000)
})

onUnmounted(() => {
  templateAbort?.abort()
  templateVersion++
  if (timer !== undefined) {
    window.clearInterval(timer)
    timer = undefined
  }
})
</script>

<template>
  <DesktopTitleBar />
  <div class="app-shell" :class="{ 'desktop-shell': isDesktop }">
    <aside class="sidebar">
      <div class="brand">
        <span class="brand-mark">M</span>
        <div>
          <strong>Mail Group</strong>
          <small>群发控制台</small>
        </div>
      </div>
      <nav>
        <button :class="{ active: activeSection === 'home' }" type="button" @click="switchSection('home')">
          首页
        </button>
        <button :class="{ active: activeSection === 'mail-content' }" type="button" @click="switchSection('mail-content')">
          邮件内容
        </button>
        <button :class="{ active: activeSection === 'target-mails' }" type="button" @click="switchSection('target-mails')">
          目标邮箱
        </button>
        <button :class="{ active: activeSection === 'sender-cluster' }" type="button" @click="switchSection('sender-cluster')">
          邮箱集群
        </button>
        <button :class="{ active: activeSection === 'data-panel' }" type="button" @click="switchSection('data-panel')">
          数据面板
        </button>
        <button :class="{ active: activeSection === 'help' }" type="button" @click="switchSection('help')">
          使用说明
        </button>
      </nav>
    </aside>

    <main class="workspace">
      <header class="topbar">
        <div>
          <h1>邮件自动发送系统</h1>
          <p>多 QQ 邮箱并行发送，收件人去重后按发送邮箱均衡分配。</p>
        </div>
      </header>

      <p v-if="errorMessage" class="notice">{{ errorMessage }}</p>
      <p v-if="recipientInputDirty && activeSection === 'home'" class="notice">
        收件人输入已修改，请先到“目标邮箱”完成解析，再开始群发。
      </p>

      <section v-if="activeSection === 'home'" class="module-section">
        <div class="section-heading">
          <span>00</span>
          <div>
            <h2>首页</h2>
            <p>查看所有群发任务，实时跟踪发送进度。</p>
          </div>
        </div>
        <div class="panel">
          <div class="panel-title inline">
            <div>
              <h2>发送邮件列表</h2>
              <span>共 {{ tasks.length }} 个任务，搜索结果 {{ filteredTasks.length }} 个</span>
            </div>
            <div class="home-actions">
              <div class="search-wrapper">
                <svg class="search-icon" viewBox="0 0 16 16" fill="currentColor" width="14" height="14">
                  <path d="M11.742 10.344a6.5 6.5 0 10-1.398 1.398l3.85 3.85a1 1 0 001.414-1.414l-3.866-3.834zm-5.242.156a4.5 4.5 0 110-9 4.5 4.5 0 010 9z"/>
                </svg>
                <input v-model="taskSearch" placeholder="搜索任务、收件人或发送邮箱" @input="taskPage = 1" />
              </div>
              <button class="primary add-sender-btn" :disabled="!canSend || loading" @click="submitTask">
                <svg class="start-icon" viewBox="0 0 16 16" fill="currentColor">
                  <path d="M4 2.5v11l9-5.5z"/>
                </svg>
                {{ loading ? '创建中...' : '开始群发' }}
              </button>
            </div>
          </div>
          <div class="table-wrap task-table">
            <table>
              <thead>
                <tr>
                  <th>任务编号</th>
                  <th>分配收件数</th>
                  <th>进度</th>
                  <th>状态</th>
                  <th>更新时间</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="task in pagedTasks" :key="task.task_id" @click="currentTaskId = task.task_id; showTaskDetail = true">
                  <td><strong>{{ task.subject || '历史任务' }}</strong><br /><small>{{ task.task_id.slice(0, 10) }}</small></td>
                  <td>{{ task.total }}</td>
                  <td>
                    <div class="task-progress-text">{{ task.success + task.failed }} / {{ task.total }}</div>
                    <div class="progress-bar mini">
                      <i :style="{ width: `${task.progress}%` }"></i>
                    </div>
                  </td>
                  <td><span class="status" :class="task.status">{{ statusText(task.status) }}</span>
                    <button v-if="task.status === 'interrupted' && task.resumable" class="text-button" @click.stop="resumeTask(task)">继续未发送</button>
                    <button v-if="task.status === 'interrupted' && task.uncertain" class="text-button" @click.stop="resumeTask(task, true)">核对后重试 {{ task.uncertain }} 封</button>
                  </td>
                  <td>{{ formatDate(task.updated_at) }}</td>
                </tr>
                <tr v-if="!pagedTasks.length">
                  <td colspan="5" class="empty-row">暂无发送任务，完善邮件内容、目标邮箱和邮箱集群后可开始群发。</td>
                </tr>
              </tbody>
            </table>
          </div>
          <div class="pagination">
            <span>每页 8 条 / 总任务数 {{ tasks.length }}</span>
            <div>
              <button class="pager" :disabled="taskPage === 1" @click="changeTaskPage(taskPage - 1)">上一页</button>
              <strong>{{ taskPage }} / {{ taskTotalPages }}</strong>
              <button class="pager" :disabled="taskPage === taskTotalPages" @click="changeTaskPage(taskPage + 1)">下一页</button>
            </div>
          </div>
        </div>
      </section>

      <section v-else-if="activeSection === 'mail-content'" class="module-section">
        <div class="section-heading">
          <span>01</span>
          <div>
            <h2>邮件内容</h2>
            <p>设置本次群发的主题、正文和附件。可保存常用邮件模板以便复用。</p>
          </div>
        </div>
        <div class="mail-content-layout">
          <div class="panel">
            <div class="panel-title inline">
              <div>
                <h2>内容编辑</h2>
              </div>
              <div class="editor-actions">
                <button class="primary add-sender-btn" :disabled="!content.subject.trim() || !content.body.trim() || templateLoading || Boolean(templateError)" @click="saveCurrentMail">
                  保存
                </button>
              </div>
            </div>
            <label>邮件主题</label>
            <input v-model="content.subject" placeholder="请输入邮件主题" />
            <label>正文格式</label>
            <select v-model="content.content_type" aria-label="正文格式"><option value="plain">纯文本</option><option value="html">HTML</option></select>
            <label>邮件正文</label>
            <textarea v-model="content.body" class="mail-body" placeholder="输入需要群发的邮件内容"></textarea>

            <label>附件（可选）</label>
            <div
              class="upload-zone"
              @dragover.prevent
              @drop.prevent="handleAttachmentDrop"
            >
              <input id="attachment-input" :disabled="templateLoading" multiple type="file" @change="handleAttachmentChange" />
              <label for="attachment-input" class="upload-trigger">
                <strong>选择本地文件</strong>
                <span>或将文件拖拽到此处。单个附件不超过 50MB，已拦截 exe、bat、cmd、js、vbs、msi 等高风险类型。</span>
              </label>
            </div>
            <p v-if="templateLoading" class="field-hint" role="status">正在加载模板附件，完成后即可发送…</p>
            <p v-if="templateError" class="field-error" role="alert">{{ templateError }}</p>
            <p v-if="attachmentError" class="field-error">{{ attachmentError }}</p>
            <div v-if="attachments.length" class="attachment-list">
              <div v-for="(file, index) in attachments" :key="`${file.name}-${file.size}`" class="attachment-item">
                <span>{{ file.name }}</span>
                <small>{{ formatFileSize(file.size) }}</small>
                <button class="text-button danger-text" @click="removeAttachment(index)">移除</button>
              </div>
            </div>
          </div>

          <div class="panel">
            <div class="panel-title">
              <h2>选择邮件模板</h2>
              <span>共 {{ savedMails.length }} 个模板，点击选择使用</span>
            </div>
            <div class="saved-mail-list">
              <div
                v-for="mail in savedMails"
                :key="mail.id"
                class="saved-mail-item"
                :class="{ active: selectedSavedMailId === mail.id }"
                @click="selectSavedMail(mail)"
              >
                <input
                  type="radio"
                  :checked="selectedSavedMailId === mail.id"
                  class="saved-mail-radio"
                  name="saved-mail-select"
                  @click.stop
                  @change="selectSavedMail(mail)"
                />
                <div class="saved-mail-info">
                  <strong>{{ mail.subject || '(无主题)' }}</strong>
                  <span>{{ formatDate(mail.created_at) }}</span>
                </div>
                <div class="saved-mail-actions">
                  <button
                    class="icon-button detail-btn"
                    title="查看详情"
                    @click.stop="viewMailDetail(mail)"
                  >
                    <svg viewBox="0 0 16 16" fill="currentColor" width="16" height="16">
                      <path d="M8 3C4.5 3 1.6 5.2 0 8c1.6 2.8 4.5 5 8 5s6.4-2.2 8-5c-1.6-2.8-4.5-5-8-5zm0 8a3 3 0 110-6 3 3 0 010 6zm0-4.5a1.5 1.5 0 100 3 1.5 1.5 0 000-3z"/>
                    </svg>
                  </button>
                  <button
                    class="icon-button delete-btn"
                    title="删除"
                    @click.stop="confirmDeleteSavedMail(mail.id)"
                  >
                    <svg viewBox="0 0 16 16" fill="currentColor" width="16" height="16">
                      <path d="M5 1.5V3h6V1.5a.5.5 0 00-.5-.5h-5a.5.5 0 00-.5.5zM14 3H2v1.5h1.5l.7 9.3a1.5 1.5 0 001.5 1.4h4.6a1.5 1.5 0 001.5-1.4l.7-9.3H14V3z"/>
                    </svg>
                  </button>
                </div>
              </div>
              <p v-if="!savedMails.length" class="empty-row">暂无保存的邮件模板，编辑内容后点击"保存"按钮。</p>
            </div>
          </div>
        </div>
      </section>

      <section v-else-if="activeSection === 'target-mails'" class="module-section">
        <div class="section-heading">
          <span>02</span>
          <div>
            <h2>目标邮箱</h2>
            <p>录入收件邮箱，系统会在前后端同时做去重分配。</p>
          </div>
        </div>
        <div class="target-layout">
          <div class="panel">
            <div class="panel-title inline">
              <div>
                <h2>收件人列表</h2>
                <span>共 {{ recipients.length }} 个邮箱，搜索结果 {{ filteredRecipients.length }} 个</span>
              </div>
              <div class="search-box">
                <div class="search-wrapper">
                  <svg class="search-icon" viewBox="0 0 16 16" fill="currentColor" width="14" height="14">
                    <path d="M11.742 10.344a6.5 6.5 0 10-1.398 1.398l3.85 3.85a1 1 0 001.414-1.414l-3.866-3.834zm-5.242.156a4.5 4.5 0 110-9 4.5 4.5 0 010 9z"/>
                  </svg>
                  <input v-model="recipientSearch" placeholder="搜索邮箱" @input="recipientPage = 1" />
                </div>
                <div class="single-add">

                  <button class="primary add-sender-btn" @click="openAddRecipientModal">
                    <svg viewBox="0 0 16 16" fill="currentColor" width="14" height="14">
                      <path d="M8 2a.75.75 0 01.75.75v4.5h4.5a.75.75 0 010 1.5h-4.5v4.5a.75.75 0 01-1.5 0v-4.5h-4.5a.75.75 0 010-1.5h4.5v-4.5A.75.75 0 018 2z"/>
                    </svg>
                    添加收件人
                  </button>
                </div>
                <div class="batch-inline">
                  <button class="batch-btn-inline enable-all" @click="batchToggleRecipients(true)">全部启用</button>
                  <button class="batch-btn-inline disable-all" @click="batchToggleRecipients(false)">全部禁用</button>
                </div>
              </div>
              <p v-if="recipientError" class="field-error" style="margin: 6px 0 0">{{ recipientError }}</p>
            </div>
            <div class="table-wrap recipient-table">
              <table>
                <thead>
                  <tr>
                    <th>序号</th>
                    <th>邮箱地址</th>
                    <th>备注</th>
                    <th>
                      状态
                      <span
                        class="help-icon-wrap small"
                        @mouseenter="showRecipientStatusHelp = true"
                        @mouseleave="showRecipientStatusHelp = false"
                      >?</span>
                      <span v-if="showRecipientStatusHelp" class="help-tip recipient-status-tip">已成功发送的邮件会自动转换为禁用状态</span>
                    </th>
                    <th>分配发送邮箱</th>
                    <th>操作</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="(recipient, index) in pagedRecipients" :key="recipient.id">
                    <td>{{ (recipientPage - 1) * recipientPageSize + index + 1 }}</td>
                    <td>{{ recipient.email }}</td>
                    <td class="note-cell">{{ recipient.note || '-' }}</td>
                    <td>
                      <label class="toggle-switch" @click.prevent="toggleRecipient(recipient.id)">
                        <input type="checkbox" :checked="recipient.enabled" />
                        <span class="toggle-slider"></span>
                        <span class="toggle-label">{{ recipient.enabled ? '启用' : '禁用' }}</span>
                      </label>
                    </td>
                    <td>{{ previewAssignments.find((item) => item.recipient === recipient.email)?.sender || '-' }}</td>
                    <td>
                      <button class="text-button" @click="openEditRecipientModal(recipient)">编辑</button>
                      <button class="text-button danger-text" @click="removeRecipient(recipient.email)">删除</button>
                    </td>
                  </tr>
                  <tr v-if="!pagedRecipients.length">
                    <td colspan="6" class="empty-row">暂无收件人，请在下方批量输入或单条添加。</td>
                  </tr>
                </tbody>
              </table>
            </div>
            <div class="pagination">
              <span>每页 8 条 / 总邮件数 {{ recipients.length }}</span>
              <div>
                <button class="pager" :disabled="recipientPage === 1" @click="changeRecipientPage(recipientPage - 1)">上一页</button>
                <strong>{{ recipientPage }} / {{ recipientTotalPages }}</strong>
                <button class="pager" :disabled="recipientPage === recipientTotalPages" @click="changeRecipientPage(recipientPage + 1)">下一页</button>
              </div>
            </div>
          </div>

          <div class="grid two-columns">
            <div class="panel">
              <div class="panel-title">
                <h2>收件人编辑</h2>
                <span>批量输入邮箱后点击"解析"按钮保存到数据库</span>
              </div>
              <textarea
                v-model="recipientsText"
                class="recipient-box"
                placeholder="每行一个邮箱，也可粘贴邮箱链接或 Markdown 表格；支持逗号、分号分隔"
                @input="onRecipientsInput"
              ></textarea>
              <p v-if="recipientError" class="field-error">{{ recipientError }}</p>
              <p v-if="recipientImportMessage" role="status" class="summary-line">{{ recipientImportMessage }}</p>
              <div class="parse-bar">
                <button class="primary parse-btn" :disabled="parsingRecipients" @click="parseRecipientsText">
                  <svg viewBox="0 0 16 16" fill="currentColor" width="13" height="13">
                    <path d="M8 0a1 1 0 011 1v6h6a1 1 0 010 2H9v6a1 1 0 01-2 0V9H1a1 1 0 010-2h6V1a1 1 0 011-1z"/>
                  </svg>
                  {{ parsingRecipients ? '解析中...' : '解析' }}
                </button>
                <span class="summary-line">共 {{ recipients.length }} 个邮箱，重复已自动合并</span>
              </div>
            </div>

            <div class="panel">
              <div class="panel-title">
                <h2>分配预览</h2>
                <span>每个目标邮箱只会分配给一个发送邮箱</span>
              </div>
              <div class="table-wrap compact">
                <table>
                  <thead>
                    <tr>
                      <th>目标邮箱</th>
                      <th>发送邮箱</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr v-for="item in previewAssignments" :key="item.recipient">
                      <td>{{ item.recipient }}</td>
                      <td>{{ item.sender }}</td>
                    </tr>
                    <tr v-if="!previewAssignments.length">
                      <td colspan="2" class="empty-row">请先填写发送邮箱和目标邮箱</td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </div>
          </div>
        </div>
      </section>

      <section v-else-if="activeSection === 'sender-cluster'" class="module-section">
        <div class="section-heading">
          <span>03</span>
          <div>
            <h2>邮箱集群</h2>
            <p>配置多个 QQ SMTP 发送邮箱，提高并行发送能力。</p>
          </div>
        </div>
        <div class="panel">
          <div class="panel-title inline">
            <div>
              <h2>发送邮箱配置</h2>
              <span>QQ 邮箱需填写 SMTP 授权码，不是登录密码。</span>
            </div>
            <button class="primary add-sender-btn" @click="openAddSenderModal">
              <svg viewBox="0 0 16 16" fill="currentColor" width="14" height="14">
                <path d="M8 2a.75.75 0 01.75.75v4.5h4.5a.75.75 0 010 1.5h-4.5v4.5a.75.75 0 01-1.5 0v-4.5h-4.5a.75.75 0 010-1.5h4.5v-4.5A.75.75 0 018 2z"/>
              </svg>
              添加邮箱
            </button>
          </div>

          <div class="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>邮箱地址</th>
                  <th>SMTP 地址</th>
                  <th>端口</th>
                  <th>备注</th>
                  <th>状态</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="sender in savedSenders" :key="sender.id">
                  <td>{{ sender.email }}</td>
                  <td>{{ sender.smtp_host }}</td>
                  <td>{{ sender.smtp_port }}</td>
                  <td>{{ sender.note || '-' }}</td>
                  <td>
                    <label class="toggle-switch" @click.prevent="toggleSender(sender)">
                      <input type="checkbox" :checked="sender.enabled" />
                      <span class="toggle-slider"></span>
                      <span class="toggle-label">{{ sender.enabled ? '启用' : '禁用' }}</span>
                    </label>
                  </td>
                  <td>
                    <button class="text-button" @click="openEditSenderModal(sender)">编辑</button>
                    <button class="text-button danger-text" @click="removeSender(String(sender.id))">删除</button>
                  </td>
                </tr>
                <tr v-if="!savedSenders.length">
                  <td colspan="6" class="empty-row">暂无发送邮箱配置，点击"添加邮箱"按钮新增。</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </section>

      <section v-else-if="activeSection === 'data-panel'" class="module-section">
        <div class="section-heading">
          <span>04</span>
          <div>
            <h2>数据面板</h2>
            <p>实时统计发送结果，分析发件人与收件人表现。</p>
          </div>
        </div>
        <section class="metrics">
          <div class="metric-card">
            <span>任务数</span>
            <strong>{{ dashboard.totalTasks }}</strong>
          </div>
          <div class="metric-card">
            <span>本期邮件总量</span>
            <strong>{{ totalAll }}</strong>
          </div>
          <div class="metric-card">
            <span>运行任务</span>
            <strong>{{ dashboard.running }}</strong>
          </div>
          <div class="metric-card success">
            <span>本期发送成功</span>
            <strong>
              {{ totalSuccess }}
              <small v-if="statsData && statsData.success_prev" class="trend" :class="totalSuccess >= statsData.success_prev ? 'trend-up' : 'trend-down'">
                {{ totalSuccess >= statsData.success_prev ? '↑' : '↓' }}{{ statsData.success_prev }}
              </small>
            </strong>
          </div>
          <div class="metric-card danger">
            <span>本期发送失败</span>
            <strong>
              {{ totalFailed }}
              <small v-if="statsData && statsData.failed_prev" class="trend" :class="totalFailed <= statsData.failed_prev ? 'trend-up' : 'trend-down'">
                {{ totalFailed <= statsData.failed_prev ? '↓' : '↑' }}{{ statsData.failed_prev }}
              </small>
            </strong>
          </div>
          <div class="metric-card">
            <span>本期成功率</span>
            <strong class="success-rate" :class="successRateClass">
              {{ statsData ? overallRate + '%' : '-' }}
              <small v-if="trendArrow" class="trend" :class="trendArrow.up ? 'trend-up' : 'trend-down'">
                {{ trendArrow.up ? '↑' : '↓' }}{{ trendArrow.value }}%
              </small>
            </strong>
          </div>
        </section>

        <div class="panel" v-if="runningTasks.length">
          <div class="panel-title inline">
            <div>
              <h2>发送进度</h2>
              <span>共 {{ runningTasks.length }} 个任务运行中</span>
            </div>
          </div>
          <div v-for="rt in runningTasks" :key="rt.task_id" class="task-progress-block">
            <div class="progress-head">
              <strong>{{ rt.task_id.slice(0, 8) }} · {{ rt.progress }}%</strong>
              <span>{{ statusText(rt.status) }}</span>
            </div>
            <div class="progress-bar">
              <i :style="{ width: `${rt.progress}%` }"></i>
            </div>
            <div class="result-line">
              <span>总数 {{ rt.total }}</span>
              <span>成功 {{ rt.success }}</span>
              <span>失败 {{ rt.failed }}</span>
              <span>
                待处理 {{ rt.pending }}
                <button
                  v-if="rt.pending"
                  class="pending-toggle"
                  @click="togglePendingList(rt.task_id)"
                >{{ isPendingExpanded(rt.task_id) ? '收起' : '查看' }}</button>
              </span>
            </div>
            <div v-if="isPendingExpanded(rt.task_id) && rt.pending" class="pending-recipients">
              <div class="pending-header">📋 待发送邮箱（共 {{ rt.pending }} 个）</div>
              <div class="pending-list">
                <div
                  v-for="item in rt.assignments.filter(a => a.status === 'pending' || a.status === 'sending')"
                  :key="`pending-${item.recipient}`"
                  class="pending-item"
                >
                  <span class="pending-email">{{ item.recipient }}</span>
                  <span class="pending-sender">→ {{ item.sender }}</span>
                  <span class="status" :class="item.status">{{ statusText(item.status) }}</span>
                </div>
              </div>
            </div>
            <div class="table-wrap result-table">
              <table>
                <thead>
                  <tr>
                    <th>收件人</th>
                    <th>发送邮箱</th>
                    <th>状态</th>
                    <th>结果</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="item in rt.assignments" :key="`${rt.task_id}-${item.recipient}`">
                    <td>{{ item.recipient }}</td>
                    <td>{{ item.sender }}</td>
                    <td><span class="status" :class="item.status">{{ statusText(item.status) }}</span></td>
                    <td class="msg-cell">{{ item.message || '-' }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
        </div>
        <div class="panel" v-else>
          <div class="panel-title">
            <h2>发送进度</h2>
          </div>
          <p class="empty">暂无运行中的任务，创建任务后会在这里看到实时进度。</p>
        </div>

        <div class="panel chart-section">
          <div class="panel-title inline">
            <div>
              <h2>发送统计</h2>
              <span>共 {{ totalAll }} 条记录</span>
            </div>
            <div class="period-filter">
              <button
                v-for="p in (['hour', 'day', 'week', 'month'] as StatsPeriod[])"
                :key="p"
                class="period-btn"
                :class="{ active: statsPeriod === p }"
                @click="switchStatsPeriod(p)"
              >
                {{ p === 'hour' ? '每时' : p === 'day' ? '每日' : p === 'week' ? '每周' : '每月' }}
              </button>
            </div>
          </div>

          <template v-if="gridItems">
            <div class="panel chart-card">
              <div class="panel-title">
                <h2>发送量 &amp; 成功率趋势</h2>
              </div>
              <div class="chart-area">
                <div class="chart-y-axis" :key="'ytc-'+statsPeriod">
                  <span v-for="(t, i) in getYAxisTicks(chartMax(gridItems, 'total'))" :key="'ytc-'+i">{{ t }}</span>
                </div>
                <div class="chart-body">
                  <div class="chart-content chart-content-combo">
                    <div class="chart-grid-lines" :key="'ylc-'+statsPeriod">
                      <span v-for="(t, i) in getYAxisTicks(chartMax(gridItems, 'total'))" :key="'ylc-'+i"></span>
                    </div>
                    <div class="chart-bars">
                      <div v-for="item in gridItems" :key="'bc-'+item.label" class="chart-col">
                        <div class="chart-tooltip">
                          <strong>{{ formatTooltipLabel(item.label, statsPeriod) }}</strong>
                          <span>总量 {{ item.total }}</span>
                          <span>成功 {{ item.success }}</span>
                          <span>失败 {{ item.failed }}</span>
                          <span>成功率 {{ item.total ? ((item.success / item.total) * 100).toFixed(1) + '%' : '-' }}</span>
                        </div>
                        <div class="chart-stack">
                          <div class="chart-bar-success" :style="{ height: barHeight(item.success, chartMax(gridItems, 'total')) }"></div>
                          <div class="chart-bar-fail" :style="{ height: barHeight(item.failed, chartMax(gridItems, 'total')) }"></div>
                          <span class="chart-val chart-val-top" v-if="item.total">{{ item.total }}</span>
                        </div>
                      </div>
                    </div>
                    <div class="chart-rate-line" :key="'crl-'+statsPeriod">
                      <svg class="chart-rate-svg" viewBox="0 0 100 100" preserveAspectRatio="none">
                        <defs>
                          <linearGradient id="successRateLineGradient" x1="0" y1="0" x2="100" y2="0" gradientUnits="userSpaceOnUse">
                            <stop offset="0%" stop-color="#38bdf8" />
                            <stop offset="52%" stop-color="#2563eb" />
                            <stop offset="100%" stop-color="#7c3aed" />
                          </linearGradient>
                        </defs>
                        <path
                          class="chart-rate-path-glow"
                          :d="rateLinePath(gridItems)"
                          fill="none"
                          stroke="#60a5fa"
                          stroke-width="4.8"
                          stroke-linecap="round"
                          stroke-linejoin="round"
                          vector-effect="non-scaling-stroke"
                        />
                        <path
                          class="chart-rate-path"
                          :d="rateLinePath(gridItems)"
                          fill="none"
                          stroke="url(#successRateLineGradient)"
                          stroke-width="2.4"
                          stroke-linecap="round"
                          stroke-linejoin="round"
                          vector-effect="non-scaling-stroke"
                        />
                      </svg>
                      <div
                        v-for="(item, index) in gridItems"
                        :key="'cr-'+item.label"
                        class="chart-rate-dot"
                        :style="{
                          left: dotLeftPct(index, gridItems.length) + '%',
                          bottom: item.total ? Math.max((item.success / item.total) * 100, 4) + '%' : '0%',
                        }"
                      >
                        <div class="chart-rate-tooltip">
                          <strong>{{ formatTooltipLabel(item.label, statsPeriod) }}</strong>
                          <span>成功率 {{ item.total ? ((item.success / item.total) * 100).toFixed(1) + '%' : '-' }}</span>
                          <span>成功 {{ item.success }} / 失败 {{ item.failed }}</span>
                        </div>
                      </div>
                    </div>
                    <div class="chart-rate-y-axis" :key="'yrc-'+statsPeriod">
                      <span v-for="(t, i) in getYAxisTicks(100)" :key="'yrc-'+i">{{ t }}%</span>
                    </div>
                  </div>
                  <div class="chart-x-axis">
                    <span v-for="item in gridItems" :key="'xlc-'+item.label">{{ chartLabel(item.label, statsPeriod) }}</span>
                  </div>
                </div>
                <div class="chart-legend">
                  <span class="legend-item"><i class="legend-dot legend-success"></i>成功</span>
                  <span class="legend-item"><i class="legend-dot legend-fail"></i>失败</span>
                  <span class="legend-item"><i class="legend-line"></i>成功率</span>
                </div>
              </div>
            </div>
          </template>
          <p v-else class="empty">暂无发送记录，开始群发后数据将在此展示。</p>
        </div>

        <div class="grid two-columns">
          <div class="panel" v-if="senderBreakdown.length">
            <div class="panel-title">
              <h2>发件人表现</h2>
              <span>按发送邮箱统计</span>
            </div>
            <div class="table-wrap compact">
              <table>
                <thead>
                  <tr>
                    <th>发送邮箱</th>
                    <th>总量</th>
                    <th>成功</th>
                    <th>失败</th>
                    <th>成功率</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="s in senderBreakdown" :key="s.sender">
                    <td>{{ s.sender }}</td>
                    <td>{{ s.total }}</td>
                    <td>{{ s.success }}</td>
                    <td>{{ s.failed }}</td>
                    <td>
                      <span class="rate-badge" :class="s.success_rate >= 90 ? 'rate-high' : s.success_rate >= 70 ? 'rate-mid' : 'rate-low'">
                        {{ s.success_rate }}%
                      </span>
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>

          <div class="panel" v-if="failureReasons.length">
            <div class="panel-title">
              <h2>失败原因分布</h2>
              <span>共 {{ failureReasons.reduce((s, r) => s + r.count, 0) }} 次失败</span>
            </div>
            <div class="pie-chart-wrap">
              <div
                class="pie-chart"
                :style="{ background: pieChartStyle() }"
                @mousemove="handlePieMouseMove"
                @mouseleave="handlePieMouseLeave"
              >
                <template v-if="pieSliceList().length">
                  <div
                    v-for="(slice, idx) in pieSliceList()"
                    :key="`slice-${idx}`"
                    class="pie-slice"
                    :class="{ active: pieHover?.index === idx }"
                    :style="{
                      background: `conic-gradient(${slice.color} ${slice.start}% ${slice.end}%, transparent ${slice.end}% 100%, transparent 0% ${slice.start}%)`,
                      clipPath: `polygon(${slice.clipPoints})`,
                      '--slice-translate': slice.translate,
                      '--slice-rotate': slice.rotate,
                      '--slice-color': slice.color,
                    }"
                  ></div>
                </template>
                <div class="pie-chart-hole">
                  <div class="pie-chart-total">{{ failureReasons.reduce((s, r) => s + r.count, 0) }}</div>
                  <div class="pie-chart-label">总失败</div>
                </div>
                <div
                  v-if="pieHover"
                  class="pie-tooltip"
                  :style="{ left: pieHover.x + 'px', top: pieHover.y + 'px' }"
                >
                  <div class="pie-tooltip-name">{{ translateErrorMessage(failureReasons[pieHover.index]?.reason) }}</div>
                  <div class="pie-tooltip-data">{{ failureReasons[pieHover.index]?.count ?? 0 }} 次 · {{ (failureReasons[pieHover.index]?.percentage ?? 0).toFixed(1) }}%</div>
                </div>
              </div>
              <div class="pie-chart-legend">
                <div v-for="(r, idx) in failureReasons" :key="r.reason" class="pie-legend-item" :class="{ active: pieHover?.index === idx }">
                  <span class="pie-legend-dot" :style="{ background: pieColor(idx) }"></span>
                  <span class="pie-legend-text">{{ translateErrorMessage(r.reason) }}</span>
                  <span class="pie-legend-count">{{ r.count }} 次</span>
                  <span class="pie-legend-percent">{{ r.percentage.toFixed(1) }}%</span>
                </div>
              </div>
            </div>
          </div>
        </div>

        <div class="panel" v-if="failedRecipientsTotal > 0 || failedRecipients.length">
          <div class="panel-title">
            <div>
              <h2>发送失败邮箱列表</h2>
              <span>共 {{ failedRecipientsTotal }} 条失败记录 · 当前 {{ failedRecipientRange }} / 第 {{ failedRecipientPage }} / {{ failedRecipientTotalPages }} 页</span>
            </div>
          </div>
          <button class="export-btn" @click="exportFailedRecipients">📄 导出 TXT</button>
          <div class="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>收件人邮箱</th>
                  <th>发送邮箱</th>
                  <th>失败原因</th>
                  <th>时间</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(fr, i) in failedRecipients" :key="`${fr.recipient}-${i}`">
                  <td>{{ fr.recipient }}</td>
                  <td>{{ fr.sender }}</td>
                  <td class="msg-cell" :title="fr.message">
                    <span v-if="fr.message" class="error-badge">失败</span>
                    {{ translateErrorMessage(fr.message) }}
                  </td>
                  <td>{{ formatDate(fr.finished_at) }}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <div class="pagination" v-if="failedRecipientTotalPages > 1">
            <button
              class="page-btn"
              :disabled="failedRecipientPage <= 1"
              @click="changeFailedRecipientPage(failedRecipientPage - 1)"
            >上一页</button>
            <button
              v-for="p in failedRecipientPages"
              :key="p.label + '-' + p.value"
              class="page-btn"
              :class="{ active: isPageActive(p), ellipsis: p.value === null }"
              :disabled="p.value === null"
              @click="p.value !== null && changeFailedRecipientPage(p.value)"
            >{{ p.label }}</button>
            <button
              class="page-btn"
              :disabled="failedRecipientPage >= failedRecipientTotalPages"
              @click="changeFailedRecipientPage(failedRecipientPage + 1)"
            >下一页</button>
            <span class="page-jump">
              <span class="page-jump-label">跳至</span>
              <input
                v-model="failedRecipientJump"
                class="page-jump-input"
                type="text"
                inputmode="numeric"
                maxlength="4"
                placeholder="页数"
                @keyup.enter="jumpFailedRecipientPage"
              />
              <button class="page-btn page-go-btn" @click="jumpFailedRecipientPage">GO</button>
            </span>
          </div>
        </div>
      </section>

      <section v-if="activeSection === 'help'" class="module-section">
        <div class="section-heading">
          <span>05</span>
          <div>
            <h2>使用说明</h2>
            <p>了解如何配置邮箱、获取授权码以及使用本系统。</p>
          </div>
        </div>
        <div class="panel">
          <div class="panel-title">
            <h2>系统使用教程</h2>
            <span>从配置到发送，一步到位掌握群发操作</span>
          </div>
          <div class="help-card">
            <div class="help-step">
              <span class="help-num">1</span>
              <div>
                <strong>配置发送邮箱（邮箱集群）</strong>
                <p>进入 <b>"邮箱集群"</b> 页面，点击 <b>"添加发送邮箱"</b>，填写 QQ 邮箱地址、SMTP 授权码、SMTP 服务器地址（默认 smtp.qq.com）和端口（默认 465）。可以添加多个发送邮箱，系统会自动轮询分配。</p>
              </div>
            </div>
            <div class="help-step">
              <span class="help-num">2</span>
              <div>
                <strong>添加目标邮箱</strong>
                <p>进入 <b>"目标邮箱"</b> 页面，可以通过单条添加或批量粘贴的方式录入收件人邮箱。每个邮箱可附带备注，方便管理。系统会自动去重，避免重复发送。</p>
              </div>
            </div>
            <div class="help-step">
              <span class="help-num">3</span>
              <div>
                <strong>编写邮件内容</strong>
                <p>进入 <b>"邮件内容"</b> 页面，填写邮件主题和正文。支持纯文本和 HTML 两种格式，可添加附件（单个不超过 50MB）。编辑完成后点击 <b>"保存邮件"</b>。</p>
              </div>
            </div>
            <div class="help-step">
              <span class="help-num">4</span>
              <div>
                <strong>预览分配并发送</strong>
                <p>回到 <b>"首页"</b>，在 <b>"发送邮件"</b> 区域可以预览每次发送的邮件分配情况（哪个邮箱发给哪个收件人）。确认无误后点击 <b>"开始发送"</b>，系统将并行发送邮件，实时显示进度。</p>
              </div>
            </div>
            <div class="help-step">
              <span class="help-num">5</span>
              <div>
                <strong>查看数据面板</strong>
                <p>进入 <b>"数据面板"</b>，可以查看发送进度、发送量 &amp; 成功率趋势图、失败原因分布饼图以及失败邮箱列表。支持按小时/天/周/月切换统计维度，失败邮箱可一键导出 TXT 文件。</p>
              </div>
            </div>
          </div>
        </div>
        <div class="panel">
          <div class="panel-title">
            <h2>如何获取 QQ 邮箱 SMTP 授权码</h2>
            <span>配置发送邮箱时的授权码不是 QQ 密码</span>
          </div>
          <div class="help-card">
            <div class="help-step">
              <span class="help-num">1</span>
              <div>
                <strong>登录 QQ 邮箱</strong>
                <p>打开 <a href="https://mail.qq.com" target="_blank">mail.qq.com</a>，使用你的 QQ 账号登录邮箱。</p>
              </div>
            </div>
            <div class="help-step">
              <span class="help-num">2</span>
              <div>
                <strong>进入设置页面</strong>
                <p>点击顶部导航栏的 <b>"设置"</b> → <b>"账号与安全"</b> → <b>"安全设置"</b>。</p>
              </div>
            </div>
            <div class="help-step">
              <span class="help-num">3</span>
              <div>
                <strong>生成授权码</strong>
                <p>在 <b>"安全设置"</b> 中找到 <b>"生成授权码"</b> 按钮并点击，按提示通过手机短信验证。生成的 <b>16 位字母授权码</b> 就是你需要填写的 SMTP 授权码。</p>
              </div>
            </div>
            <div class="help-step">
              <span class="help-num">4</span>
              <div>
                <strong>填写到本系统</strong>
                <p>复制授权码，回到 <b>"邮箱集群"</b> 页面，添加邮箱时将授权码粘贴到 <b>"授权码"</b> 输入框中即可。</p>
              </div>
            </div>
          </div>
          <div class="help-note">
            <svg viewBox="0 0 16 16" fill="currentColor" width="14" height="14">
              <path d="M8 1a7 7 0 110 14A7 7 0 018 1zm0 1.5a5.5 5.5 0 100 11 5.5 5.5 0 000-11zm.5 7V12h-1V9.5h1zm-.5-4a1.5 1.5 0 011.5 1.5c0 .6-.3 1.1-.7 1.4l-.3.2A1 1 0 007.5 10h1a1.8 1.8 0 00.5-2.7 1.5 1.5 0 00-2.5-1A1.5 1.5 0 007 7.5H6a2.5 2.5 0 113.2-2.4A2.5 2.5 0 018 7.5z"/>
            </svg>
            <span>QQ 邮箱 SMTP 服务器地址为 <code>smtp.qq.com</code>，端口使用 <code>465</code>（SSL 加密）。如果你使用的是其他邮箱（如 163、Gmail），请使用对应的 SMTP 地址和端口。</span>
          </div>
        </div>
      </section>

      <Teleport to="body">
        <div v-if="showSenderModal" class="modal-overlay" @click.self="showSenderModal = false">
          <div class="modal-panel">
            <div class="modal-head">
              <h2>{{ editingSenderId ? '编辑发送邮箱' : '添加发送邮箱' }}</h2>
              <button class="modal-close" @click="showSenderModal = false">&times;</button>
            </div>
            <div class="modal-body">
              <label>邮箱类型</label>
              <select v-model="selectedPreset" @change="applyPreset">
                <option value="">自定义</option>
                <option v-for="p in emailPresets" :key="p.type" :value="p.type">{{ p.label }}</option>
              </select>
              <label>邮箱地址</label>
              <input v-model="senderForm.email" placeholder="example@qq.com" />
              <label @mouseenter="showHelpTooltip = true" @mouseleave="showHelpTooltip = false" class="auth-label">
                SMTP授权码
                <span class="help-icon-wrap" @click.stop="goToHelp">
                  ?
                </span>
                <span v-if="showHelpTooltip" class="help-tip" @click.stop="goToHelp">如何获取授权码？</span>
              </label>
              <input v-model="senderForm.auth_code" type="password" autocomplete="new-password" :placeholder="editingSenderId ? '留空保留原授权码' : '授权码'" />
              <label>SMTP 地址</label>
              <input v-model="senderForm.smtp_host" />
              <label>端口</label>
              <input v-model.number="senderForm.smtp_port" type="number" />
              <label>备注</label>
              <input v-model="senderForm.note" placeholder="可选，用于识别该邮箱" />
            </div>
            <div class="modal-foot">
              <button class="ghost" @click="showSenderModal = false">取消</button>
              <button class="primary" :disabled="!senderForm.email.trim() || (!editingSenderId && !senderForm.auth_code.trim())" @click="submitSenderForm">
                {{ editingSenderId ? '保存' : '添加' }}
              </button>
            </div>
          </div>
        </div>
      </Teleport>

      <Teleport to="body">
        <div v-if="showEditRecipientModal" class="modal-overlay" @click.self="showEditRecipientModal = false">
          <div class="modal-panel">
            <div class="modal-head">
              <h2>编辑收件人</h2>
              <button class="modal-close" @click="showEditRecipientModal = false">&times;</button>
            </div>
            <div class="modal-body">
              <label>邮箱地址</label>
              <input v-model="editRecipientEmail" placeholder="收件人邮箱" />
              <label>备注</label>
              <input v-model="editRecipientNote" placeholder="可选备注" />
            </div>
            <div class="modal-foot">
              <button class="ghost" @click="showEditRecipientModal = false">取消</button>
              <button class="primary" :disabled="!editRecipientEmail.trim()" @click="confirmEditRecipient">保存</button>
            </div>
          </div>
        </div>
      </Teleport>

      <Teleport to="body">
        <div v-if="showRecipientModal" class="modal-overlay" @click.self="showRecipientModal = false">
          <div class="modal-panel">
            <div class="modal-head">
              <h2>添加收件人</h2>
              <button class="modal-close" @click="showRecipientModal = false">&times;</button>
            </div>
            <div class="modal-body">
              <label>邮箱地址</label>
              <input v-model="modalRecipientEmail" placeholder="请输入收件人邮箱" @keyup.enter="confirmAddRecipient" />
              <label>备注</label>
              <input v-model="modalRecipientNote" placeholder="可选备注" />
              <p v-if="modalRecipientError" class="field-error">{{ modalRecipientError }}</p>
            </div>
            <div class="modal-foot">
              <button class="ghost" @click="showRecipientModal = false">取消</button>
              <button class="primary" :disabled="!modalRecipientEmail.trim()" @click="confirmAddRecipient">确认</button>
            </div>
          </div>
        </div>
      </Teleport>

      <Teleport to="body">
        <div v-if="showDetailModal && detailMail" class="modal-overlay" @click.self="showDetailModal = false">
          <div class="modal-panel">
            <div class="modal-head">
              <h2>邮件详情</h2>
              <button class="modal-close" @click="showDetailModal = false">&times;</button>
            </div>
            <div class="modal-body">
              <label>主题</label>
              <div class="detail-field">{{ detailMail.subject }}</div>
              <label>内容</label>
              <div class="detail-field detail-body">{{ detailMail.body }}</div>
              <label>保存时间</label>
              <div class="detail-field">{{ formatDate(detailMail.created_at) }}</div>
              <label>附件（{{ detailMail.attachments.length }}）</label>
              <div v-if="detailMail.attachments.length" class="detail-attachments">
                <div v-for="att in detailMail.attachments" :key="att" class="detail-attachment-item">
                  <svg viewBox="0 0 16 16" fill="currentColor" width="14" height="14">
                    <path d="M4 1h5l4 4v9.5a1 1 0 01-1 1H4a1 1 0 01-1-1V2a1 1 0 011-1zm5 .5V5h3.5L9 1.5z"/>
                  </svg>
                  <span>{{ att.split('_').slice(1).join('_') }}</span>
                  <button class="text-button download-link" @click="downloadAttachment(detailMail, att)">下载</button>
                </div>
              </div>
              <p v-else class="detail-field" style="color:#94a3b8">无附件</p>
            </div>
            <div class="modal-foot">
              <button class="primary" @click="showDetailModal = false">关闭</button>
            </div>
          </div>
        </div>
      </Teleport>
      <Teleport to="body">
        <div v-if="showTaskDetail && selectedTask" class="modal-overlay" @click.self="showTaskDetail = false">
          <div class="modal-panel task-detail-dialog" role="dialog" aria-modal="true" aria-label="任务详情">
            <div class="modal-head"><h2>{{ selectedTask.subject || '任务详情' }}</h2><button class="modal-close" aria-label="关闭任务详情" @click="showTaskDetail = false">&times;</button></div>
            <div class="modal-body">
              <p>{{ statusText(selectedTask.status) }} · 成功 {{ selectedTask.success }} · 失败 {{ selectedTask.failed }} · 待处理 {{ selectedTask.pending }}</p>
              <p v-if="selectedTask.uncertain" class="field-error">{{ selectedTask.uncertain }} 封邮件的送达状态尚未确认，请先在发件箱核对，避免重复发送。</p>
              <div class="table-wrap"><table><thead><tr><th>收件人</th><th>发件人</th><th>结果</th><th>说明</th></tr></thead>
                <tbody><tr v-for="item in selectedTask.assignments" :key="item.recipient"><td>{{ item.recipient }}</td><td>{{ item.sender }}</td><td>{{ statusText(item.status) }}</td><td>{{ item.message || '-' }}</td></tr>
                <tr v-if="!selectedTask.assignments.length"><td colspan="4">此旧版任务没有可用的发送明细。</td></tr></tbody></table></div>
            </div>
            <div class="modal-foot"><button class="primary" @click="showTaskDetail = false">关闭</button></div>
          </div>
        </div>
      </Teleport>
    </main>
  </div>
</template>

<style scoped>
:global(*) {
  box-sizing: border-box;
}

:global(body) {
  margin: 0;
  background: #f4f6f8;
  color: #202733;
  font-family:
    Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}

.desktop-shell { padding-top: 42px; }
.desktop-shell .sidebar { top: 42px; height: calc(100vh - 42px); }
.task-detail-dialog { width: min(1000px, 92vw); max-width: 1000px; }
.task-detail-dialog .modal-body { max-height: 65vh; overflow: auto; }
.status.interrupted, .status.uncertain { color: #9a5a16; background: #fff3df; }
.field-hint { color: #56718d; font-size: 13px; }
button:focus-visible, a:focus-visible { outline: 2px solid #517fae; outline-offset: 3px; }

.app-shell {
  display: flex;
  min-height: 100vh;
}

.sidebar {
  width: 228px;
  flex-shrink: 0;
  padding: 24px 18px;
  background: #182230;
  color: #d9e1eb;
  position: sticky;
  top: 0;
  align-self: flex-start;
  height: 100vh;
  overflow-y: auto;
}

.brand {
  display: flex;
  gap: 12px;
  align-items: center;
  margin-bottom: 34px;
}

.brand-mark {
  display: grid;
  width: 38px;
  height: 38px;
  place-items: center;
  border-radius: 10px;
  background: #eff6ff;
  color: #1d4ed8;
  font-weight: 700;
}

.brand strong,
.brand small {
  display: block;
}

.brand small {
  margin-top: 3px;
  color: #93a3b8;
}

nav {
  display: grid;
  gap: 8px;
}

nav button {
  width: 100%;
  padding: 11px 12px;
  border-radius: 9px;
  background: transparent;
  color: #aeb9c8;
  text-align: left;
}

nav button.active,
nav button:hover {
  background: #243247;
  color: #fff;
}

.workspace {
  flex: 1;
  min-width: 0;
  padding: 26px;
  overflow: auto;
}

.topbar,
.panel-title.inline,
.sender-head,
.summary-line,
.progress-head,
.result-line {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}

.topbar {
  margin-bottom: 20px;
}

h1,
h2,
p {
  margin: 0;
}

h1 {
  font-size: 24px;
}

h2 {
  font-size: 18px;
}

.topbar p,
.panel-title span,
.summary-line,
.result-line,
.empty {
  margin-top: 6px;
  color: #6b7280;
  font-size: 13px;
}

button,
input,
select,
textarea {
  font: inherit;
}

button {
  border: 0;
  cursor: pointer;
}

button:disabled {
  cursor: not-allowed;
  opacity: 0.55;
}

.primary,
.ghost {
  height: 38px;
  padding: 0 16px;
  border-radius: 8px;
}

.primary {
  display: inline-flex;
  gap: 8px;
  align-items: center;
  background: #16a34a;
  color: #fff;
}

.start-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 18px;
  height: 18px;
  font-size: 10px;
}

.ghost {
  border: 1px solid #d8dee8;
  background: #fff;
  color: #2f3a4a;
}

.text-button {
  background: transparent;
  color: #7b8494;
}

.module-section {
  margin-bottom: 22px;
}

.section-heading {
  display: flex;
  gap: 12px;
  align-items: flex-start;
  margin-bottom: 14px;
}

.section-heading > span {
  display: grid;
  min-width: 34px;
  height: 28px;
  place-items: center;
  border-radius: 7px;
  background: #e8eef8;
  color: #315482;
  font-size: 12px;
  font-weight: 700;
}

.section-heading p {
  margin-top: 4px;
  color: #6b7280;
  font-size: 13px;
}

.metrics {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 14px;
  margin-bottom: 16px;
}

.toggle-switch {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  cursor: pointer;
}

.toggle-switch input {
  display: none;
}

.toggle-slider {
  width: 28px;
  height: 16px;
  border-radius: 8px;
  background: #cbd5e1;
  position: relative;
  transition: background 0.15s;
  flex-shrink: 0;
}

.toggle-slider::after {
  content: '';
  position: absolute;
  top: 2px;
  left: 2px;
  width: 12px;
  height: 12px;
  border-radius: 50%;
  background: #fff;
  transition: left 0.15s;
}

.toggle-switch input:checked + .toggle-slider {
  background: #4ade80;
}

.toggle-switch input:checked + .toggle-slider::after {
  left: 14px;
}

.toggle-label {
  font-size: 11px;
  color: #94a3b8;
  white-space: nowrap;
}

.sender-card,
.metric-card,
.panel {
  border: 1px solid #e4e8ef;
  border-radius: 12px;
  background: #fff;
}

.panel {
  padding: 18px;
  position: relative;
}

.metric-card {
  padding: 16px;
}

.sender-card {
  padding: 16px;
}

.metric-card span {
  display: block;
  color: #6b7280;
  font-size: 13px;
}

.metric-card strong {
  display: block;
  margin-top: 8px;
  font-size: 25px;
}

.metric-card.success strong {
  color: #15803d;
}

.metric-card.danger strong {
  color: #b91c1c;
}

.grid {
  display: grid;
  gap: 16px;
}

.two-columns {
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
}

.bottom-grid {
  margin-top: 16px;
}

.panel-title {
  margin-bottom: 18px;
}

.export-btn {
  position: absolute;
  top: 12px;
  right: 12px;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 8px 20px;
  border: 1px solid #86efac;
  border-radius: 8px;
  background: linear-gradient(135deg, #f0fdf4 0%, #dcfce7 100%);
  color: #15803d;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  transition: background 0.15s, border-color 0.15s, box-shadow 0.15s;
  white-space: nowrap;
  box-shadow: 0 1px 3px rgba(22, 101, 52, 0.12);
}

.export-btn:hover {
  background: linear-gradient(135deg, #dcfce7 0%, #bbf7d0 100%);
  border-color: #4ade80;
  box-shadow: 0 2px 8px rgba(22, 101, 52, 0.18);
}

label {
  display: block;
  margin: 12px 0 7px;
  color: #475569;
  font-size: 13px;
  font-weight: 600;
}

input,
select,
textarea {
  width: 100%;
  border: 1px solid #d8dee8;
  border-radius: 8px;
  outline: none;
  background: #fff;
  color: #202733;
}

input,
select {
  height: 38px;
  padding: 0 11px;
}

textarea {
  min-height: 120px;
  padding: 11px;
  resize: vertical;
  line-height: 1.55;
}

input:focus,
select:focus,
textarea:focus {
  border-color: #7aa7ff;
  box-shadow: 0 0 0 3px rgb(37 99 235 / 10%);
}

.mail-body,
.recipient-box {
  min-height: 220px;
}

.target-layout {
  display: grid;
  gap: 16px;
}

.home-actions {
  display: grid;
  grid-template-columns: minmax(260px, 1fr) auto;
  gap: 10px;
  align-items: center;
}

.search-box {
  display: flex;
  gap: 10px;
  align-items: center;
  min-width: 0;
}

.search-box .search-wrapper {
  width: 200px;
  flex-shrink: 0;
}

.search-box .single-add {
  display: flex;
  gap: 6px;
  flex: 1;
  min-width: 0;
  margin-top: 0;
}

.search-box .single-add input {
  flex: 1;
  min-width: 0;
}

.parse-btn {
  display: inline-flex;
  gap: 6px;
  align-items: center;
  height: 34px;
  font-size: 13px;
  padding: 0 14px;
}

.parse-bar {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-top: 10px;
}

.parse-bar .summary-line {
  color: #64748b;
  font-size: 13px;
  display: inline;
}

.task-table tbody tr {
  cursor: pointer;
}

.task-table tbody tr:hover {
  background: #f8fbff;
}

.task-progress-text {
  color: #475569;
  font-size: 12px;
}

.recipient-table {
  min-height: 385px;
}

.pagination {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
  margin-top: 14px;
  color: #64748b;
  font-size: 13px;
}

.pagination > div {
  display: inline-flex;
  gap: 10px;
  align-items: center;
}

.pager {
  height: 32px;
  padding: 0 12px;
  border: 1px solid #d8dee8;
  border-radius: 7px;
  background: #fff;
  color: #334155;
}

.danger-text {
  color: #b91c1c;
}

.upload-zone {
  position: relative;
  display: grid;
  min-height: 128px;
  place-items: center;
  border: 1px dashed #a8b4c6;
  border-radius: 10px;
  background: #f8fafc;
  text-align: center;
}

.upload-zone input {
  position: absolute;
  width: 1px;
  height: 1px;
  opacity: 0;
}

.upload-trigger {
  max-width: 520px;
  margin: 0;
  padding: 20px;
  cursor: pointer;
}

.upload-trigger strong,
.upload-trigger span {
  display: block;
}

.upload-trigger strong {
  color: #1d4ed8;
  font-size: 15px;
}

.upload-trigger span {
  margin-top: 8px;
  color: #64748b;
  font-weight: 400;
  line-height: 1.6;
}

.field-error {
  margin-top: 8px;
  color: #b91c1c;
  font-size: 13px;
}

.attachment-list {
  display: grid;
  gap: 8px;
  margin-top: 12px;
}

.attachment-item {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto auto;
  gap: 12px;
  align-items: center;
  padding: 10px 12px;
  border: 1px solid #edf0f5;
  border-radius: 8px;
  background: #fff;
}

.attachment-item span {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.attachment-item small {
  color: #64748b;
}

.progress-bar.mini {
  height: 7px;
  margin: 6px 0 0;
}

.sender-list {
  display: grid;
  gap: 12px;
}

.sender-card {
  padding: 14px;
  background: #fbfcfe;
}

.form-row {
  display: grid;
  grid-template-columns: 1fr 1.2fr 1.1fr 1fr 90px;
  gap: 12px;
}

.notice {
  margin-bottom: 14px;
  padding: 10px 12px;
  border: 1px solid #fecaca;
  border-radius: 8px;
  background: #fff1f2;
  color: #b91c1c;
}

.table-wrap {
  overflow: auto;
  border: 1px solid #edf0f5;
  border-radius: 9px;
}

.note-cell {
  max-width: 160px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: #64748b;
}

.table-wrap.compact {
  max-height: 330px;
}

.pending-toggle {
  background: none;
  border: none;
  color: #3b82f6;
  font-size: 12px;
  cursor: pointer;
  padding: 1px 6px;
  border-radius: 3px;
  transition: background 0.15s;
}

.pending-toggle:hover {
  background: #eff6ff;
}

.pending-recipients {
  margin-top: 12px;
  border: 1px solid #e2e8f0;
  border-radius: 8px;
  overflow: hidden;
  animation: pendingFadeIn 0.25s ease;
}

@keyframes pendingFadeIn {
  from { opacity: 0; transform: translateY(-4px); }
  to { opacity: 1; transform: translateY(0); }
}

.pending-header {
  padding: 8px 14px;
  background: #fffbeb;
  color: #92400e;
  font-weight: 600;
  font-size: 13px;
  border-bottom: 1px solid #fde68a;
}

.pending-list {
  max-height: 260px;
  overflow-y: auto;
}

.pending-item {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 7px 14px;
  font-size: 13px;
  border-bottom: 1px solid #f1f5f9;
  transition: background 0.15s;
}

.pending-item:last-child {
  border-bottom: none;
}

.pending-item:hover {
  background: #f8fafc;
}

.pending-email {
  flex: 1;
  color: #1e293b;
  font-weight: 500;
  word-break: break-all;
}

.pending-sender {
  color: #94a3b8;
  font-size: 12px;
  white-space: nowrap;
}

.result-table {
  max-height: 320px;
  margin-top: 14px;
}

table {
  width: 100%;
  border-collapse: collapse;
  font-size: 14px;
}

th,
td {
  padding: 12px 14px;
  border-bottom: 1px solid #edf0f5;
  text-align: left;
  vertical-align: middle;
}

th {
  background: #f8fafc;
  color: #64748b;
  font-weight: 600;
}

.progress-bar {
  height: 9px;
  margin: 12px 0;
  overflow: hidden;
  border-radius: 999px;
  background: #e8edf5;
}

.progress-bar i {
  display: block;
  height: 100%;
  border-radius: inherit;
  background: #2563eb;
  transition: width 0.25s ease;
}

.status {
  display: inline-block;
  padding: 3px 8px;
  border-radius: 999px;
  background: #eef2f7;
  color: #475569;
}

.status.success {
  background: #dcfce7;
  color: #166534;
}

.status.failed {
  background: #fee2e2;
  color: #991b1b;
}

.status.sending,
.status.running {
  background: #dbeafe;
  color: #1d4ed8;
}

.mail-content-layout {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
  align-items: start;
}

.editor-actions {
  display: flex;
  gap: 8px;
}

.saved-mail-list {
  display: grid;
  gap: 8px;
  max-height: 520px;
  overflow: auto;
}

.saved-mail-item {
  display: grid;
  grid-template-columns: auto 1fr auto;
  gap: 10px;
  align-items: center;
  padding: 10px 12px;
  border: 1px solid #edf0f5;
  border-radius: 8px;
  cursor: pointer;
  transition: border-color 0.15s, background 0.15s;
}

.saved-mail-item:hover {
  border-color: #7aa7ff;
  background: #f8fbff;
}

.saved-mail-item.active {
  border-color: #2563eb;
  background: #eff6ff;
}

.saved-mail-actions {
  display: flex;
  gap: 8px;
  opacity: 0;
  transition: opacity 0.15s;
}

.saved-mail-item:hover .saved-mail-actions {
  opacity: 1;
}

.saved-mail-item.active .saved-mail-actions {
  opacity: 1;
}

.icon-button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 34px;
  height: 34px;
  border: 0;
  border-radius: 8px;
  background: transparent;
  cursor: pointer;
  color: #64748b;
  transition: background 0.15s, color 0.15s;
}

.icon-button:hover {
  background: #f1f5f9;
}

.icon-button.delete-btn:hover {
  background: #fef2f2;
  color: #b91c1c;
}

.icon-button.detail-btn:hover {
  background: #eff6ff;
  color: #1d4ed8;
}

.saved-mail-radio {
  width: 16px;
  height: 16px;
  margin: 0;
  cursor: pointer;
  accent-color: #2563eb;
}

.saved-mail-info {
  overflow: hidden;
  min-width: 0;
}

.saved-mail-info strong {
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 14px;
}

.saved-mail-info span {
  display: block;
  margin-top: 2px;
  color: #94a3b8;
  font-size: 12px;
}

.saved-mail-delete {
  flex-shrink: 0;
}

.detail-field {
  padding: 10px 12px;
  border: 1px solid #e4e8ef;
  border-radius: 8px;
  background: #f8fafc;
  font-size: 14px;
  line-height: 1.6;
  color: #202733;
  white-space: pre-wrap;
  word-break: break-word;
}

.detail-body {
  min-height: 160px;
  max-height: 320px;
  overflow: auto;
}

.detail-attachments {
  display: grid;
  gap: 6px;
}

.detail-attachment-item {
  display: flex;
  gap: 10px;
  align-items: center;
  padding: 8px 10px;
  border: 1px solid #e4e8ef;
  border-radius: 8px;
  background: #f8fafc;
  font-size: 13px;
  color: #475569;
}

.detail-attachment-item span {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.download-link {
  flex-shrink: 0;
  color: #2563eb;
  text-decoration: none;
  font-weight: 600;
}

.download-link:hover {
  text-decoration: underline;
}

.search-wrapper {
  position: relative;
  display: flex;
  align-items: center;
}

.search-icon {
  position: absolute;
  left: 11px;
  color: #94a3b8;
  pointer-events: none;
  flex-shrink: 0;
}

.search-wrapper input {
  padding-left: 32px;
}

.add-sender-btn {
  display: inline-flex;
  gap: 6px;
  align-items: center;
}

.empty-row {
  padding: 40px 12px;
  text-align: center;
  color: #94a3b8;
  font-size: 14px;
  line-height: 1.6;
}

.modal-overlay {
  position: fixed;
  inset: 0;
  display: grid;
  place-items: center;
  background: rgb(0 0 0 / 35%);
  z-index: 1000;
}

.modal-panel {
  width: 440px;
  max-width: 90vw;
  border-radius: 14px;
  background: #fff;
  box-shadow: 0 20px 60px rgb(0 0 0 / 20%);
  overflow: hidden;
}

.modal-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 18px 22px 0;
}

.modal-head h2 {
  font-size: 17px;
}

.modal-close {
  width: 30px;
  height: 30px;
  border-radius: 8px;
  background: transparent;
  color: #64748b;
  font-size: 22px;
  line-height: 1;
}

.modal-close:hover {
  background: #f1f5f9;
}

.modal-body {
  padding: 8px 22px 18px;
}

.modal-body label {
  margin: 14px 0 6px;
}

.modal-foot {
  display: flex;
  gap: 10px;
  justify-content: flex-end;
  padding: 14px 22px;
  border-top: 1px solid #edf0f5;
}

@media (max-width: 1180px) {
  .metrics,
  .two-columns,
  .mail-content-layout,
  .home-actions {
    grid-template-columns: 1fr;
  }

  .sidebar {
    width: 176px;
    padding: 20px 12px;
  }
}

.chart-section {
  margin-top: 16px;
}

.period-filter {
  display: flex;
  gap: 6px;
}

.period-btn {
  padding: 5px 12px;
  border: 1px solid #e2e6ed;
  border-radius: 6px;
  background: #fff;
  color: #475569;
  font-size: 12px;
  cursor: pointer;
  transition: border-color 0.15s, background 0.15s, color 0.15s;
}

.period-btn:hover {
  border-color: #7aa7ff;
  color: #2563eb;
}

.period-btn.active {
  border-color: #2563eb;
  background: #eff6ff;
  color: #2563eb;
  font-weight: 600;
}

.chart-card {
  margin-top: 0;
  overflow: visible;
  background:
    radial-gradient(circle at 12% 10%, rgb(37 99 235 / 6%), transparent 30%),
    linear-gradient(180deg, #ffffff 0%, #fbfdff 100%);
}

.chart-card .panel-title {
  padding-bottom: 0;
}

.chart-area {
  --chart-height: 210px;
  --chart-side-offset: 16px;
  --chart-rate-axis-width: 42px;
  display: grid;
  grid-template-columns: 42px minmax(0, 1fr) var(--chart-rate-axis-width);
  column-gap: 8px;
  row-gap: 0;
  align-items: start;
}

.chart-y-axis {
  display: flex;
  flex-direction: column-reverse;
  justify-content: space-between;
  height: var(--chart-height);
  flex-shrink: 0;
  position: relative;
  padding-top: 2px;
}

.chart-y-axis span {
  display: block;
  font-size: 10px;
  color: #94a3b8;
  text-align: right;
  padding-right: 6px;
  line-height: 1;
  transform: translateY(50%);
}

.chart-body {
  min-width: 0;
}

.chart-content {
  position: relative;
  height: var(--chart-height);
  padding: 0 var(--chart-side-offset);
  border-radius: 16px 16px 0 0;
  background:
    linear-gradient(180deg, rgb(248 250 252 / 82%), rgb(255 255 255 / 28%)),
    radial-gradient(circle at 50% 0%, rgb(37 99 235 / 8%), transparent 42%);
}

.chart-content::after {
  content: '';
  position: absolute;
  top: 0;
  right: 0;
  bottom: 0;
  width: 1px;
  background: linear-gradient(180deg, transparent, #d4dae4 8%, #d4dae4 92%, transparent);
  pointer-events: none;
}

.chart-grid-lines {
  position: absolute;
  inset: 0 var(--chart-side-offset) 0 var(--chart-side-offset);
  display: flex;
  flex-direction: column-reverse;
  justify-content: space-between;
  pointer-events: none;
  z-index: 0;
}

.chart-grid-lines span {
  display: block;
  border-top: 1px dashed #e8ecf2;
  width: 100%;
  height: 0;
}

.chart-grid-lines span:first-child {
  border-top-style: solid;
  border-top-color: #d4dae4;
}

.chart-grid-lines span:last-child {
  border-top-style: solid;
  border-top-color: #d4dae4;
}

.chart-bars {
  display: flex;
  align-items: flex-end;
  gap: 3px;
  height: var(--chart-height);
  position: relative;
  z-index: 1;
}

.chart-col {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: flex-end;
  height: 100%;
  min-width: 0;
  position: relative;
}

.chart-val {
  position: absolute;
  top: -14px;
  left: 50%;
  transform: translateX(-50%);
  font-size: 10px;
  color: #475569;
  white-space: nowrap;
  font-weight: 600;
}

.chart-tooltip {
  position: absolute;
  bottom: calc(100% + 4px);
  left: 50%;
  transform: translateX(-50%);
  padding: 6px 10px;
  border-radius: 6px;
  background: #1e293b;
  color: #f1f5f9;
  font-size: 11px;
  line-height: 1.5;
  white-space: nowrap;
  pointer-events: none;
  opacity: 0;
  z-index: 20;
}

.chart-tooltip strong {
  display: block;
  margin-bottom: 2px;
  color: #fff;
  font-weight: 600;
}

.chart-tooltip span {
  display: block;
}

.chart-col:hover .chart-tooltip {
  opacity: 1;
}

.chart-x-axis {
  display: flex;
  gap: 3px;
  padding: 10px var(--chart-side-offset) 0;
  margin-top: -1px;
  border-top: 1px solid #d4dae4;
  position: relative;
  z-index: 2;
}

.chart-x-axis span {
  flex: 1;
  min-width: 0;
  font-size: 10px;
  color: #94a3b8;
  text-align: center;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  line-height: 1.15;
}

.success-rate {
  font-size: 22px;
}

.success-rate.rate-high {
  color: #16a34a;
}

.success-rate.rate-mid {
  color: #eab308;
}

.success-rate.rate-low {
  color: #ef4444;
}

.trend {
  display: inline-block;
  margin-left: 6px;
  font-size: 12px;
  font-weight: 500;
  vertical-align: middle;
  opacity: 0.7;
}

.trend.trend-up {
  color: #16a34a;
}

.trend.trend-down {
  color: #ef4444;
}

.task-progress-block {
  border-top: 1px solid #e4e8ef;
  padding: 14px 0;
}

.task-progress-block:first-child {
  border-top: none;
  padding-top: 0;
}

.chart-content-combo {
  height: var(--chart-height);
  overflow: visible;
}

.chart-content-combo .chart-bars {
  height: 100%;
}

.chart-stack {
  width: 100%;
  max-width: 30px;
  display: flex;
  flex-direction: column;
  justify-content: flex-end;
  align-items: stretch;
  height: 100%;
  min-height: 0;
  gap: 0;
  position: relative;
}

.chart-val-top {
  position: absolute;
  top: -16px;
  left: 50%;
  transform: translateX(-50%);
  font-size: 10px;
  color: #475569;
  white-space: nowrap;
  font-weight: 600;
  line-height: 1;
}

.chart-bar-fail {
  position: relative;
  width: 100%;
  border-radius: 0 0 7px 7px;
  background: linear-gradient(180deg, #fca5a5 0%, #ef4444 100%);
  transition: height 0.3s, filter 0.1s;
  cursor: pointer;
}

.chart-bar-fail:hover {
  filter: brightness(0.85);
}

.chart-bar-success {
  position: relative;
  width: 100%;
  border-radius: 7px 7px 0 0;
  background: linear-gradient(180deg, #86efac 0%, #22c55e 100%);
  transition: height 0.3s, filter 0.1s;
  cursor: pointer;
}

.chart-bar-success:hover {
  filter: brightness(0.85);
}

.chart-rate-line {
  position: absolute;
  inset: 0 var(--chart-side-offset) 0 var(--chart-side-offset);
  z-index: 5;
  overflow: visible;
  pointer-events: none;
}

.chart-rate-svg {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  z-index: 4;
  filter: drop-shadow(0 4px 6px rgb(37 99 235 / 16%));
}

.chart-rate-path-glow {
  opacity: 0.22;
}

.chart-rate-path {
  filter: saturate(1.08);
}

.chart-rate-dot {
  position: absolute;
  width: 14px;
  height: 14px;
  pointer-events: auto;
  z-index: 6;
  transform: translate(-50%, 50%);
}

.chart-rate-dot::before {
  content: '';
  position: absolute;
  left: 50%;
  top: 50%;
  width: 9px;
  height: 9px;
  border-radius: 50%;
  background: #2563eb;
  border: 2px solid #fff;
  transform: translate(-50%, -50%);
  box-shadow: 0 0 0 1px rgb(37 99 235 / 28%);
}

.chart-rate-dot:hover::before {
  transform: translate(-50%, -50%) scale(1.15);
  background: #1d4ed8;
}

.chart-rate-tooltip {
  position: absolute;
  bottom: calc(100% + 6px);
  left: 50%;
  transform: translateX(-50%);
  padding: 6px 10px;
  border-radius: 6px;
  background: #1e293b;
  color: #f1f5f9;
  font-size: 11px;
  line-height: 1.5;
  white-space: nowrap;
  pointer-events: none;
  opacity: 0;
  z-index: 30;
}

.chart-rate-tooltip strong {
  display: block;
  margin-bottom: 2px;
  color: #fff;
  font-weight: 600;
}

.chart-rate-tooltip span {
  display: block;
}

.chart-rate-dot:hover .chart-rate-tooltip {
  opacity: 1;
}

.chart-rate-y-axis {
  position: absolute;
  top: 0;
  left: calc(100% + 8px);
  display: flex;
  flex-direction: column-reverse;
  justify-content: space-between;
  width: var(--chart-rate-axis-width);
  height: var(--chart-height);
  flex-shrink: 0;
  padding-left: 4px;
  pointer-events: none;
}

.chart-rate-y-axis span {
  display: block;
  font-size: 10px;
  color: #2563eb;
  text-align: left;
  line-height: 1;
  font-weight: 600;
  transform: translateY(50%);
}

.chart-legend {
  grid-column: 2;
  display: flex;
  gap: 10px;
  padding: 12px var(--chart-side-offset) 0;
  justify-content: center;
  align-items: center;
  flex-wrap: wrap;
  border-top: 0;
  width: 100%;
  justify-self: stretch;
  box-sizing: border-box;
}

.legend-item {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 24px;
  padding: 0 10px;
  border: 1px solid #e2e8f0;
  border-radius: 999px;
  background: rgb(255 255 255 / 78%);
  box-shadow: 0 6px 14px rgb(15 23 42 / 4%);
  font-size: 12px;
  font-weight: 600;
  color: #64748b;
}

.legend-dot {
  display: inline-block;
  width: 10px;
  height: 10px;
  border-radius: 3px;
  flex-shrink: 0;
}

.legend-success {
  background: linear-gradient(180deg, #86efac 0%, #22c55e 100%);
}

.legend-fail {
  background: linear-gradient(180deg, #fca5a5 0%, #ef4444 100%);
}

.legend-line {
  position: relative;
  display: inline-block;
  width: 24px;
  height: 10px;
  flex-shrink: 0;
}

.legend-line::before {
  content: '';
  position: absolute;
  left: 0;
  right: 0;
  top: 50%;
  height: 3px;
  border-radius: 999px;
  background: linear-gradient(90deg, #38bdf8 0%, #2563eb 52%, #7c3aed 100%);
  transform: translateY(-50%);
}

.legend-line::after {
  content: '';
  position: absolute;
  left: 50%;
  top: 50%;
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: #2563eb;
  border: 2px solid #fff;
  box-shadow: 0 0 0 1px rgb(37 99 235 / 35%);
  transform: translate(-50%, -50%);
}

.rate-badge {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 10px;
  font-size: 12px;
  font-weight: 600;
}

.rate-badge.rate-high {
  background: #dcfce7;
  color: #15803d;
}

.rate-badge.rate-mid {
  background: #fef9c3;
  color: #a16207;
}

.rate-badge.rate-low {
  background: #fee2e2;
  color: #b91c1c;
}

.msg-cell {
  max-width: 240px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: #475569;
  display: flex;
  align-items: center;
  gap: 6px;
}

.error-badge {
  display: inline-flex;
  align-items: center;
  background: #fee2e2;
  color: #b91c1c;
  font-size: 10px;
  padding: 2px 8px;
  border-radius: 6px;
  font-weight: 600;
  flex-shrink: 0;
}

/* 饼图样式 */
.pie-chart-wrap {
  display: flex;
  gap: 24px;
  align-items: center;
  flex-wrap: wrap;
}

.pie-chart {
  width: 160px;
  height: 160px;
  border-radius: 50%;
  position: relative;
  flex-shrink: 0;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.06);
  cursor: pointer;
  overflow: visible;
  transition: box-shadow 0.35s ease, transform 0.35s ease;
}

.pie-chart:has(.pie-slice.active) {
  box-shadow: 0 4px 20px rgba(0, 0, 0, 0.12), 0 0 0 4px rgba(148, 163, 184, 0.12);
  transform: scale(1.02);
}

.pie-slice {
  position: absolute;
  inset: 0;
  border-radius: 50%;
  pointer-events: none;
  opacity: 0;
  transform-origin: 50% 50%;
  will-change: transform, opacity, filter;
  transition: opacity 0.12s ease;
}

@keyframes pieSliceActive {
  0% {
    transform: translate(0, 0) scale(1) rotate(0deg);
    filter: brightness(1) saturate(1) drop-shadow(0 0 0 transparent);
  }
  40% {
    transform: var(--slice-translate) scale(1.08) var(--slice-rotate);
    filter: brightness(1.15) saturate(1.3) drop-shadow(0 2px 14px var(--slice-color));
  }
  70% {
    transform: var(--slice-translate) scale(1.04) var(--slice-rotate);
    filter: brightness(1.04) saturate(1.1) drop-shadow(0 0 6px var(--slice-color));
  }
  100% {
    transform: var(--slice-translate) scale(1.055) var(--slice-rotate);
    filter: brightness(1.06) saturate(1.12) drop-shadow(0 0 8px var(--slice-color));
  }
}

.pie-slice.active {
  opacity: 1;
  animation: pieSliceActive 0.45s cubic-bezier(0.22, 0.61, 0.36, 1) forwards;
}

.pie-tooltip {
  position: absolute;
  pointer-events: none;
  z-index: 10;
  transform: translate(-50%, -130%);
  padding: 6px 10px;
  border-radius: 6px;
  background: #1e293b;
  color: #f1f5f9;
  font-size: 11px;
  line-height: 1.5;
  white-space: nowrap;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
}

.pie-tooltip::after {
  content: '';
  position: absolute;
  top: 100%;
  left: 50%;
  transform: translateX(-50%);
  border: 5px solid transparent;
  border-top-color: #1e293b;
}

.pie-tooltip-name {
  font-weight: 600;
  color: #ffffff;
  margin-bottom: 2px;
}

.pie-tooltip-data {
  color: #cbd5e1;
}

.pie-chart-hole {
  position: absolute;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  width: 90px;
  height: 90px;
  border-radius: 50%;
  background: #ffffff;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
}

.pie-chart-total {
  font-size: 20px;
  font-weight: 700;
  color: #1e293b;
  line-height: 1.2;
}

.pie-chart-label {
  font-size: 11px;
  color: #94a3b8;
  margin-top: 2px;
}

.pie-chart-legend {
  flex: 1;
  min-width: 180px;
}

.pie-legend-item {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  transition: background 0.15s;
  border-radius: 4px;
  padding: 5px 6px;
}

.pie-legend-item.active {
  background: #f1f5f9;
}

.pie-legend-dot {
  width: 10px;
  height: 10px;
  border-radius: 2px;
  flex-shrink: 0;
}

.pie-legend-text {
  color: #475569;
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  padding-right: 6px;
}

.pie-legend-count {
  color: #64748b;
  flex-shrink: 0;
  min-width: 40px;
  text-align: right;
}

.pie-legend-percent {
  color: #1e293b;
  font-weight: 600;
  flex-shrink: 0;
  min-width: 48px;
  text-align: right;
}

.batch-inline {
  display: flex;
  gap: 6px;
  align-items: center;
  flex-shrink: 0;
  margin-left: 4px;
}

.batch-btn-inline {
  height: 34px;
  padding: 0 14px;
  border-radius: 8px;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  border: 1px solid;
  transition: background 0.15s, border-color 0.15s, color 0.15s, box-shadow 0.15s;
  white-space: nowrap;
  display: inline-flex;
  align-items: center;
  gap: 5px;
}

.batch-btn-inline.enable-all {
  background: linear-gradient(135deg, #f0fdf4 0%, #dcfce7 100%);
  border-color: #86efac;
  color: #15803d;
  box-shadow: 0 1px 3px rgba(22, 101, 52, 0.1);
}

.batch-btn-inline.enable-all::before {
  content: '🔓';
  font-size: 12px;
}

.batch-btn-inline.enable-all:hover {
  background: linear-gradient(135deg, #dcfce7 0%, #bbf7d0 100%);
  border-color: #4ade80;
  box-shadow: 0 2px 8px rgba(22, 101, 52, 0.15);
}

.batch-btn-inline.disable-all {
  background: linear-gradient(135deg, #fef2f2 0%, #fee2e2 100%);
  border-color: #fecaca;
  color: #991b1b;
  box-shadow: 0 1px 3px rgba(153, 27, 27, 0.1);
}

.batch-btn-inline.disable-all::before {
  content: '🔒';
  font-size: 12px;
}

.batch-btn-inline.disable-all:hover {
  background: linear-gradient(135deg, #fee2e2 0%, #fca5a5 100%);
  border-color: #f87171;
  box-shadow: 0 2px 8px rgba(153, 27, 27, 0.15);
}

/* 分页控件 */
.pagination {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  padding: 12px 0 4px 0;
}

.page-btn {
  min-width: 34px;
  height: 32px;
  padding: 0 10px;
  border: 1px solid #e2e8f0;
  background: #ffffff;
  color: #475569;
  border-radius: 6px;
  font-size: 12px;
  cursor: pointer;
  transition: all 0.15s;
}

.page-btn:hover:not(:disabled) {
  background: #f8fafc;
  border-color: #cbd5e1;
  color: #1e293b;
}

.page-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.page-btn.active {
  background: #3b82f6;
  border-color: #3b82f6;
  color: #ffffff;
  font-weight: 600;
}

.page-btn.ellipsis {
  border-color: transparent;
  background: transparent;
  color: #94a3b8;
  cursor: default;
  min-width: 28px;
  padding: 0 4px;
}

.page-jump {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  margin-left: 6px;
}

.page-jump-label {
  font-size: 12px;
  color: #94a3b8;
}

.page-jump-input {
  width: 48px;
  height: 32px;
  border: 1px solid #e2e8f0;
  border-radius: 6px;
  text-align: center;
  font-size: 12px;
  color: #475569;
  outline: none;
  transition: border-color 0.15s;
}

.page-jump-input:focus {
  border-color: #3b82f6;
  box-shadow: 0 0 0 2px rgba(59, 130, 246, 0.15);
}

.page-go-btn {
  min-width: 36px;
  padding: 0 8px;
  font-weight: 600;
  color: #3b82f6;
  border-color: #bfdbfe;
}

.page-go-btn:hover:not(:disabled) {
  background: #eff6ff;
  border-color: #3b82f6;
}

.auth-label {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  position: relative;
  cursor: default;
}

.help-icon-wrap {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 18px;
  height: 18px;
  border-radius: 50%;
  border: 1.5px solid #94a3b8;
  color: #94a3b8;
  font-size: 11px;
  font-weight: 700;
  line-height: 1;
  cursor: pointer;
  transition: border-color 0.15s, color 0.15s, background 0.15s;
}

.help-icon-wrap:hover {
  border-color: #2563eb;
  color: #2563eb;
  background: #eff6ff;
}

.help-icon-wrap.small {
  width: 15px;
  height: 15px;
  font-size: 10px;
  margin-left: 3px;
  vertical-align: middle;
}

.recipient-status-tip {
  left: 50%;
  transform: translateX(-50%);
  white-space: nowrap;
  font-size: 11px;
}

th:has(.help-icon-wrap) {
  position: relative;
}

.help-tip {
  position: absolute;
  top: 100%;
  left: 0;
  margin-top: 4px;
  padding: 4px 10px;
  border-radius: 6px;
  background: #1e293b;
  color: #f1f5f9;
  font-size: 12px;
  font-weight: 400;
  white-space: nowrap;
  cursor: pointer;
  z-index: 10;
}

.help-tip:hover {
  text-decoration: underline;
}

.help-card {
  display: grid;
  gap: 16px;
  padding: 16px 20px 20px;
}

.help-step {
  display: flex;
  gap: 14px;
  align-items: flex-start;
}

.help-num {
  display: grid;
  width: 28px;
  height: 28px;
  place-items: center;
  border-radius: 50%;
  background: #2563eb;
  color: #fff;
  font-size: 14px;
  font-weight: 700;
  flex-shrink: 0;
}

.help-step strong {
  display: block;
  margin-bottom: 4px;
  font-size: 14px;
  color: #1e293b;
}

.help-step p {
  margin: 0;
  font-size: 13px;
  color: #475569;
  line-height: 1.6;
}

.help-step a {
  color: #2563eb;
  text-decoration: none;
}

.help-step a:hover {
  text-decoration: underline;
}

.help-note {
  display: flex;
  gap: 8px;
  align-items: flex-start;
  margin: 0 20px 20px;
  padding: 12px 14px;
  border-radius: 8px;
  background: #fff7ed;
  color: #9a3412;
  font-size: 13px;
  line-height: 1.6;
}

.help-note code {
  padding: 1px 5px;
  border-radius: 4px;
  background: #fed7aa;
  font-size: 12px;
}

.preset-row {
  display: flex;
  gap: 10px;
  margin-bottom: 0;
}

.preset-select-wrap {
  flex: 1;
}

.preset-label {
  font-size: 12px;
  color: #64748b;
  margin: 0 0 4px 0;
  font-weight: 500;
}
</style>
