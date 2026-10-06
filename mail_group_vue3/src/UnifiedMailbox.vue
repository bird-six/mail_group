<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { request } from './api'

interface Account {
  id: number; email: string; note: string; configured: boolean
  synced_at: string; last_error: string; cached: number; remaining: number
}
interface Mail {
  id: number; sender_id: number; account_email: string; folder_kind: 'inbox' | 'sent'
  subject: string; from_text: string; to_text: string; message_date: string; unread: number; size: number
  source?: 'imap' | 'local'
}
interface Detail extends Mail { cc_text: string; body: string; attachments: string[]; truncated: boolean }
interface MailPage { items: Mail[]; total: number; page: number; page_size: number }
const emit = defineEmits<{ configure: [id: number] }>()
const accounts = ref<Account[]>([])
const accountId = ref('')
const kind = ref('all')
const searchText = ref('')
const query = ref('')
const page = ref(1)
const result = ref<MailPage>({ items: [], total: 0, page: 1, page_size: 30 })
const selectedId = ref<number | null>(null)
const detail = ref<Detail | null>(null)
const listLoading = ref(false)
const detailLoading = ref(false)
const syncing = ref(false)
const errors = ref('')
const detailError = ref('')
const syncMessage = ref('')
const progress = ref<Record<number, string>>({})
const pages = computed(() => Math.max(1, Math.ceil(result.value.total / result.value.page_size)))
const targets = computed(() => accounts.value.filter(a => !accountId.value || a.id === Number(accountId.value)))
const remaining = computed(() => targets.value.reduce((sum, a) => sum + a.remaining, 0))
let listVersion = 0
let detailVersion = 0
let disposed = false

function date(value: string) {
  return value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '尚未同步'
}
async function loadAccounts() {
  accounts.value = await request<Account[]>('/api/mailbox/accounts')
}
async function refresh() {
  clearDetail()
  try { await loadAccounts(); await loadMessages() }
  catch (error) { errors.value = error instanceof Error ? error.message : '刷新列表失败' }
}
async function loadMessages() {
  const version = ++listVersion
  listLoading.value = true
  errors.value = ''
  const params = new URLSearchParams({ kind: kind.value, query: query.value, page: String(page.value) })
  if (accountId.value) params.set('sender_id', accountId.value)
  try {
    const next = await request<MailPage>(`/api/mailbox/messages?${params}`)
    if (version === listVersion && !disposed) {
      result.value = next
      if (page.value > pages.value) page.value = pages.value
    }
  } catch (error) {
    if (version === listVersion && !disposed) {
      errors.value = error instanceof Error ? error.message : '读取邮件列表失败'
      result.value = { items: [], total: 0, page: page.value, page_size: 30 }
    }
  } finally {
    if (version === listVersion && !disposed) listLoading.value = false
  }
}
async function openMail(mail: Mail) {
  const version = ++detailVersion
  selectedId.value = mail.id
  detail.value = null
  detailError.value = ''
  detailLoading.value = true
  try {
    const next = await request<Detail>(`/api/mailbox/messages/${mail.id}`)
    if (version === detailVersion && !disposed) detail.value = next
  } catch (error) {
    if (version === detailVersion && !disposed) detailError.value = error instanceof Error ? error.message : '读取正文失败'
  } finally {
    if (version === detailVersion && !disposed) detailLoading.value = false
  }
}
function clearDetail() {
  detailVersion++
  selectedId.value = null
  detail.value = null
  detailError.value = ''
  detailLoading.value = false
}
async function synchronize() {
  if (syncing.value) return
  syncing.value = true
  syncMessage.value = ''
  progress.value = {}
  const queue = [...targets.value]
  let done = 0, failed = 0, added = 0
  const total = queue.length
  async function worker() {
    while (queue.length && !disposed) {
      const account = queue.shift()!
      progress.value[account.id] = '同步中…'
      try {
        const status = await request<{ added: number; error: string }>(`/api/mailbox/sync/${account.id}`, { method: 'POST' })
        added += status.added
        if (status.error) failed++
        progress.value[account.id] = status.error || `完成 · 新增 ${status.added} 封`
      } catch (error) {
        failed++
        progress.value[account.id] = error instanceof Error ? error.message : '同步失败'
      }
      done++
      syncMessage.value = `已处理 ${done}/${total} 个邮箱 · 新增 ${added} 封${failed ? ` · ${failed} 个需检查` : ''}`
    }
  }
  try {
    await Promise.all(Array.from({ length: Math.min(3, total) }, () => worker()))
    if (!disposed) {
      clearDetail()
      await loadAccounts()
      await loadMessages()
    }
  } catch (error) {
    errors.value = error instanceof Error ? error.message : '刷新同步结果失败'
  } finally {
    syncing.value = false
  }
}
watch([accountId, kind, query], () => {
  clearDetail()
  if (page.value !== 1) page.value = 1
  else void loadMessages()
})
watch(page, () => { clearDetail(); void loadMessages() })
onMounted(async () => {
  try { await loadAccounts(); await loadMessages() }
  catch (error) { errors.value = error instanceof Error ? error.message : '加载邮箱失败' }
})
onUnmounted(() => { disposed = true; listVersion++; detailVersion++ })
</script>

<template>
  <section class="unified-mailbox" aria-label="邮件汇总">
    <header class="mailbox-heading">
      <div><h2>邮件汇总</h2><p>所有发送账号的收件箱与已发送邮件，在这里统一查看。</p></div>
      <div class="mailbox-toolbar"><button :disabled="listLoading || syncing" @click="refresh">刷新列表</button><button class="sync-mail" :disabled="syncing || !targets.length" @click="synchronize">
        {{ syncing ? '正在同步…' : accountId ? '同步当前邮箱' : '同步全部邮箱' }}
      </button></div>
    </header>
    <p class="mailbox-help">需在邮箱服务商处开启 IMAP，使用与发送相同的授权码。每次为每个文件夹补充最多 100 封，优先同步新邮件，可继续同步历史邮件。关闭群发的账号也会纳入汇总。</p>
    <p v-if="syncMessage" role="status" class="sync-result">{{ syncMessage }}</p>
    <p v-if="errors" role="alert" class="mailbox-error">{{ errors }}</p>
    <details class="account-status" :open="accounts.some(a => !a.configured || a.last_error) || result.total === 0 || syncing">
      <summary>{{ accounts.length }} 个邮箱 · 同步状态与设置<span v-if="remaining"> · 还有 {{ remaining }} 封待同步</span></summary>
      <div v-if="!accounts.length" class="mailbox-empty">请先在“邮箱集群”添加发送邮箱。</div>
      <div v-for="account in accounts" :key="account.id" class="account-status-row">
        <div><strong>{{ account.note || account.email }}</strong><small v-if="account.note">{{ account.email }}</small></div>
        <div class="account-sync-info"><span>{{ progress[account.id] || account.last_error || (account.configured ? `已缓存 ${account.cached} 封` : '待配置 IMAP 地址') }}</span><small>最近同步：{{ date(account.synced_at) }}</small></div>
        <button class="configure-account" :disabled="syncing" @click="emit('configure', account.id)">设置</button>
      </div>
    </details>
    <div class="mailbox-filters">
      <label>账号<select v-model="accountId" aria-label="筛选邮箱账号" :disabled="syncing"><option value="">全部邮箱</option><option v-for="a in accounts" :key="a.id" :value="String(a.id)">{{ a.email }}</option></select></label>
      <label>类型<select v-model="kind" aria-label="筛选收发类型"><option value="all">全部邮件</option><option value="inbox">收件箱</option><option value="sent">已发送</option></select></label>
      <form class="mailbox-search" @submit.prevent="query = searchText.trim()"><input v-model="searchText" aria-label="搜索汇总邮件" placeholder="搜索主题、发件人、收件人" maxlength="200" /><button type="submit">搜索</button></form>
    </div>
    <div class="mailbox-columns">
      <div class="mailbox-list-pane">
        <div class="mailbox-list-label">共 {{ result.total }} 封已同步邮件 <span v-if="listLoading">· 加载中…</span></div>
        <div v-if="!result.items.length && !listLoading" class="mailbox-empty">{{ query || accountId || kind !== 'all' ? '没有符合筛选条件的邮件。' : '还没有邮件。点击“同步全部邮箱”读取收件箱和已发送邮件。' }}</div>
        <div class="mailbox-message-list" :aria-busy="listLoading">
          <button v-for="mail in result.items" :key="mail.id" class="mailbox-message" :class="{ selected: selectedId === mail.id }" @click="openMail(mail)">
            <span class="mailbox-message-top"><span class="folder-badge" :class="mail.folder_kind">{{ mail.source === 'local' ? '本机发送' : mail.folder_kind === 'inbox' ? '收件' : '已发送' }}</span><span>{{ date(mail.message_date) }}</span></span>
            <strong>{{ mail.subject || '（无主题）' }}<i v-if="mail.unread && mail.folder_kind === 'inbox'" title="同步时未读" aria-label="同步时未读"></i></strong>
            <span class="mail-correspondent">{{ mail.folder_kind === 'inbox' ? mail.from_text : mail.to_text }}</span>
            <small>所属邮箱 · {{ mail.account_email }}</small>
          </button>
        </div>
        <div class="mailbox-pagination"><button :disabled="page <= 1 || listLoading" @click="page--">上一页</button><span>{{ page }} / {{ pages }}</span><button :disabled="page >= pages || listLoading" @click="page++">下一页</button></div>
      </div>
      <article class="mailbox-detail" :aria-busy="detailLoading">
        <p v-if="detailLoading" class="mailbox-empty">正在读取邮件正文…</p>
        <div v-else-if="detailError" role="alert" class="mailbox-error">{{ detailError }}<button v-if="result.items.some(m => m.id === selectedId)" @click="openMail(result.items.find(m => m.id === selectedId)!)">重试</button></div>
        <template v-else-if="detail">
          <span class="folder-badge" :class="detail.folder_kind">{{ detail.source === 'local' ? '本机发送 · 服务器已接受' : detail.folder_kind === 'inbox' ? '收件箱' : '已发送' }}</span>
          <h3>{{ detail.subject || '（无主题）' }}</h3>
          <dl><dt>所属邮箱</dt><dd>{{ detail.account_email }}</dd><dt>发件人</dt><dd>{{ detail.from_text }}</dd><dt>收件人</dt><dd>{{ detail.to_text }}</dd><template v-if="detail.cc_text"><dt>抄送</dt><dd>{{ detail.cc_text }}</dd></template><dt>时间</dt><dd>{{ date(detail.message_date) }}</dd></dl>
          <p v-if="detail.truncated" class="mailbox-warning">邮件较大，仅展示部分内容；请到原邮箱查看完整邮件及附件。</p>
          <p class="body-privacy-note">以纯文本显示正文，不加载远程图片；查看不会更改邮箱中的已读状态。</p>
          <pre class="mailbox-body">{{ detail.body || '（该邮件没有可显示的文本正文）' }}</pre>
          <div v-if="detail.attachments.length" class="mailbox-attachments"><strong>附件（请在原邮箱打开）</strong><span v-for="(name, i) in detail.attachments" :key="i">{{ name }}</span></div>
        </template>
        <div v-else class="mailbox-detail-empty"><span class="envelope-icon" aria-hidden="true">✉</span><h3>选择一封邮件</h3><p>查看发件人、收件人和邮件正文。<br />已查看的正文会保存在本机，方便离线阅读。</p></div>
      </article>
    </div>
    <p class="mailbox-footnote">同时汇总原邮箱的已发送邮件和本应用成功发送的记录，已识别的同封副本自动合并。旧版记录可能与原邮箱副本同时显示。“本机发送”表示发信服务器已接受，最终送达情况以收件方为准。暂不支持仅允许 OAuth 登录的账号。</p>
  </section>
</template>

<style scoped>
.unified-mailbox { color: #253448; min-width: 0; }
.mailbox-toolbar { display: flex; gap: 8px; flex-wrap: wrap; }
.mailbox-heading { display: flex; align-items: center; justify-content: space-between; gap: 18px; }
h2 { font-size: 22px; margin: 0 0 7px; } .mailbox-heading p { margin: 0; color: #66768a; font-size: 13px; }
button, input, select { font: inherit; } button { cursor: pointer; padding: 9px 13px; border: 1px solid #dce3eb; border-radius: 7px; background: white; color: #33475f; } button:hover { background: #f0f5fc; } button:disabled { cursor: not-allowed; opacity: .55; } button:focus-visible, select:focus-visible, input:focus-visible { outline: 2px solid #3b82f6; outline-offset: 2px; }
.sync-mail { background: #2563eb; color: white; border-color: #2563eb; white-space: nowrap; } .sync-mail:hover { background: #1d4ed8; }
.mailbox-help, .mailbox-footnote { font-size: 12px; line-height: 1.8; color: #718097; } .mailbox-help { margin: 16px 0; } .sync-result { font-size: 13px; color: #21673b; }
.account-status { background: white; border: 1px solid #e1e7ee; border-radius: 9px; margin-bottom: 18px; padding: 12px 16px; } summary { cursor: pointer; font-size: 13px; } .account-status-row { display: grid; grid-template-columns: minmax(130px, 1fr) minmax(180px, 2fr) auto; gap: 14px; border-top: 1px solid #edf0f5; padding: 12px 0 0; margin-top: 12px; font-size: 12px; align-items: center; overflow-wrap: anywhere; } .account-status-row small { display: block; color: #7c8898; margin-top: 5px; } .account-status-row strong { font-weight: 600; } .configure-account { padding: 6px 12px; }
.mailbox-filters { display: flex; gap: 12px; align-items: flex-end; flex-wrap: wrap; margin: 18px 0; } .mailbox-filters label { display: grid; gap: 6px; font-size: 12px; color: #66768a; } select, input { padding: 9px 12px; background: white; border: 1px solid #dce3eb; border-radius: 7px; color: #253448; min-width: 0; } select { max-width: 280px; } .mailbox-search { display: flex; flex: 1; min-width: 240px; gap: 7px; } .mailbox-search input { flex: 1; width: 100%; }
.mailbox-columns { display: grid; grid-template-columns: minmax(280px, 38%) minmax(0, 1fr); border: 1px solid #dfe6ee; border-radius: 10px; overflow: hidden; background: white; min-height: 500px; }
.mailbox-list-pane { min-width: 0; border-right: 1px solid #e4eaf1; display: flex; flex-direction: column; } .mailbox-list-label { padding: 14px 18px; color: #697a90; font-size: 12px; border-bottom: 1px solid #edf0f4; } .mailbox-message-list { max-height: 620px; overflow: auto; flex: 1; }
.mailbox-message { display: flex; flex-direction: column; width: 100%; border: 0; border-bottom: 1px solid #edf0f4; border-radius: 0; padding: 16px 18px; gap: 8px; text-align: left; min-width: 0; } .mailbox-message.selected { background: #eef5ff; box-shadow: inset 3px 0 #2563eb; } .mailbox-message-top { display: flex; justify-content: space-between; gap: 10px; align-items: center; font-size: 10px; color: #7f8da0; width: 100%; flex-wrap: wrap; } .folder-badge { display: inline-block; border-radius: 4px; background: #eff4fa; color: #486b96; padding: 3px 7px; font-size: 11px; } .folder-badge.sent { color: #627360; background: #f0f5ed; } .mailbox-message strong { font-size: 14px; font-weight: 600; max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; } .mailbox-message i { display: inline-block; width: 6px; height: 6px; border-radius: 50%; margin-left: 7px; background: #2563eb; } .mail-correspondent { font-size: 12px; max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; } .mailbox-message small { color: #8090a4; font-size: 11px; overflow-wrap: anywhere; }
.mailbox-pagination { display: flex; align-items: center; justify-content: space-between; padding: 12px; gap: 8px; border-top: 1px solid #edf0f4; font-size: 12px; margin-top: auto; } .mailbox-pagination button { padding: 7px 10px; }
.mailbox-detail { padding: 28px; min-width: 0; overflow-wrap: anywhere; max-height: 740px; overflow: auto; } .mailbox-detail h3 { font-size: 20px; font-weight: 600; line-height: 1.5; margin: 15px 0 20px; } dl { display: grid; grid-template-columns: 58px minmax(0,1fr); gap: 9px 12px; font-size: 12px; line-height: 1.6; } dt { color: #8591a1; } dd { margin: 0; } .body-privacy-note { font-size: 11px; color: #8a97a8; padding: 14px 0; border-block: 1px solid #edf0f4; margin-top: 24px; } .mailbox-body { white-space: pre-wrap; overflow-wrap: anywhere; font-family: inherit; line-height: 1.9; font-size: 14px; margin: 24px 0; } .mailbox-attachments { display: grid; gap: 8px; border-top: 1px solid #edf0f4; padding-top: 18px; font-size: 12px; } .mailbox-attachments span { color: #63758b; }
.mailbox-empty { padding: 28px 20px; color: #8390a0; font-size: 13px; line-height: 1.8; } .mailbox-detail-empty { display: flex; height: 100%; min-height: 350px; flex-direction: column; justify-content: center; align-items: center; text-align: center; color: #8492a5; } .mailbox-detail-empty h3 { font-size: 16px; color: #4d6179; margin: 15px 0 6px; } .mailbox-detail-empty p { font-size: 12px; line-height: 1.9; } .envelope-icon { font-size: 34px; color: #90acd0; }
.mailbox-error, .mailbox-warning { background: #fff5ee; color: #9c5020; border: 1px solid #f5dac5; border-radius: 6px; padding: 12px; font-size: 12px; line-height: 1.7; } .mailbox-error button { margin-left: 8px; }
@media (max-width: 1100px) { .mailbox-columns { grid-template-columns: minmax(240px, 40%) minmax(0, 1fr); } .mailbox-detail { padding: 20px; } .mailbox-message { padding: 14px; } }
@media (max-width: 760px) { .mailbox-columns { grid-template-columns: 1fr; } .mailbox-message-list { max-height: 330px; } .mailbox-list-pane { border-right: 0; } .mailbox-detail { border-top: 1px solid #edf0f4; } .account-status-row { grid-template-columns: 1fr auto; } .account-sync-info { grid-column: 1; } .configure-account { grid-column: 2; grid-row: 1; } }
</style>
