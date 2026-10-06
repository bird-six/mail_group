import { afterEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import UnifiedMailbox from '../UnifiedMailbox.vue'

const account = { id: 1, email: 'team@example.com', note: '合作邮箱', configured: true, synced_at: '', last_error: '', cached: 1, remaining: 0 }
const mail = { id: 1, sender_id: 1, account_email: 'team@example.com', folder_kind: 'inbox', subject: '项目更新', from_text: '客户 <customer@example.com>', to_text: 'team@example.com', message_date: '2026-10-06T01:30:00+00:00', unread: 1, size: 400 }
const body = { ...mail, cc_text: '', body: '<script>unsafe()</script>\n正文仅为纯文本', attachments: ['说明.txt'], truncated: false }
let wrapper: VueWrapper | undefined
function response(data: unknown, status = 200) { return { ok: status < 400, status, json: async () => data, text: async () => JSON.stringify(data) } }
function server(overrides: { detail?: (id: number) => Promise<unknown>; sync?: (id: number) => Promise<unknown>; total?: number; accounts?: typeof account[] } = {}) {
  const calls: { url: URL; method: string }[] = []
  vi.stubGlobal('fetch', vi.fn(async (url: string, options: RequestInit = {}) => {
    const parsed = new URL(url, 'http://localhost')
    calls.push({ url: parsed, method: options.method ?? 'GET' })
    if (parsed.pathname === '/api/mailbox/accounts') return response(overrides.accounts ?? [account])
    if (parsed.pathname.startsWith('/api/mailbox/sync/')) return overrides.sync ? overrides.sync(Number(parsed.pathname.split('/').pop())) : response({ added: 2, error: '' })
    if (parsed.pathname === '/api/mailbox/messages') return response({ items: [mail, { ...mail, id: 2, subject: '第二封邮件' }], total: overrides.total ?? 2, page: Number(parsed.searchParams.get('page')), page_size: 30 })
    if (parsed.pathname.startsWith('/api/mailbox/messages/')) return overrides.detail ? overrides.detail(Number(parsed.pathname.split('/').pop())) : response(body)
    throw new Error('Unexpected request')
  }))
  return calls
}
async function setup() { wrapper = mount(UnifiedMailbox); await flushPromises(); return wrapper }
afterEach(() => { wrapper?.unmount(); wrapper = undefined; vi.unstubAllGlobals() })

describe('Unified mailbox workflows', () => {
  it('loads cached mail without automatically contacting mailbox providers', async () => {
    const calls = server()
    const view = await setup()
    expect(view.findAll('.mailbox-message')).toHaveLength(2)
    expect(calls.every(call => call.method === 'GET')).toBe(true)
    expect(view.text()).toContain('项目更新')
  })
  it('filters by account, direction and submitted search, resetting pagination', async () => {
    const calls = server({ total: 61 })
    const view = await setup()
    await view.findAll('.mailbox-pagination button')[1]!.trigger('click')
    await flushPromises()
    expect(calls.at(-1)!.url.searchParams.get('page')).toBe('2')
    await view.get('[aria-label="筛选邮箱账号"]').setValue('1')
    await view.get('[aria-label="筛选收发类型"]').setValue('sent')
    await view.get('[aria-label="搜索汇总邮件"]').setValue('项目 & 合作')
    await view.get('.mailbox-search').trigger('submit')
    await flushPromises()
    const params = calls.at(-1)!.url.searchParams
    expect(Object.fromEntries(params)).toMatchObject({ sender_id: '1', kind: 'sent', query: '项目 & 合作', page: '1' })
  })
  it('shows body as text and keeps account and attachments visible', async () => {
    server()
    const view = await setup()
    await view.findAll('.mailbox-message')[0]!.trigger('click')
    await flushPromises()
    expect(view.get('.mailbox-body').text()).toContain('<script>unsafe()</script>')
    expect(view.find('.mailbox-body script').exists()).toBe(false)
    expect(view.get('.mailbox-detail').text()).toContain('team@example.com')
    expect(view.get('.mailbox-attachments').text()).toContain('说明.txt')
  })
  it('ignores a stale detail response after selecting a different message', async () => {
    let finish: (value: unknown) => void = () => {}
    const pending = new Promise(resolve => { finish = resolve })
    server({ detail: async id => id === 1 ? pending : response({ ...body, id: 2, subject: '第二封邮件', body: 'new body' }) })
    const view = await setup()
    await view.findAll('.mailbox-message')[0]!.trigger('click')
    await view.findAll('.mailbox-message')[1]!.trigger('click')
    await flushPromises()
    finish(response(body))
    await flushPromises()
    expect(view.get('.mailbox-body').text()).toBe('new body')
    expect(view.get('.mailbox-detail h3').text()).toBe('第二封邮件')
  })
  it('syncs all accounts and displays partial failure without discarding successful mail', async () => {
    const calls = server({ accounts: [account, { ...account, id: 2, email: 'second@example.com' }], sync: async id => response({ added: id === 1 ? 3 : 0, error: id === 2 ? '请开启 IMAP' : '' }) })
    const view = await setup()
    await view.get('.sync-mail').trigger('click')
    await flushPromises()
    expect(calls.filter(call => call.method === 'POST')).toHaveLength(2)
    expect(view.get('.sync-result').text()).toContain('新增 3 封')
    expect(view.text()).toContain('请开启 IMAP')
    expect(view.get('.sync-mail').attributes('disabled')).toBeUndefined()
  })
  it('syncs only the selected account and prevents double submission', async () => {
    let finish: (value: unknown) => void = () => {}
    const calls = server({ accounts: [account, { ...account, id: 2 }], sync: () => new Promise(resolve => { finish = resolve }) })
    const view = await setup()
    await view.get('[aria-label="筛选邮箱账号"]').setValue('1')
    await view.get('.sync-mail').trigger('click')
    await view.get('.sync-mail').trigger('click')
    expect(calls.filter(call => call.method === 'POST')).toHaveLength(1)
    expect(view.get('.sync-mail').attributes('disabled')).toBeDefined()
    finish(response({ added: 0, error: '' }))
    await flushPromises()
  })
  it('supports retry after a detail error', async () => {
    let attempt = 0
    server({ detail: async () => ++attempt === 1 ? response({ detail: '连接超时' }, 502) : response(body) })
    const view = await setup()
    await view.findAll('.mailbox-message')[0]!.trigger('click')
    await flushPromises()
    expect(view.get('.mailbox-detail').text()).toContain('连接超时')
    await view.get('.mailbox-detail button').trigger('click')
    await flushPromises()
    expect(view.get('.mailbox-body').exists()).toBe(true)
  })
  it('opens settings for the requested account and has a usable empty state', async () => {
    server()
    const view = await setup()
    await view.get('.configure-account').trigger('click')
    expect(view.emitted('configure')).toEqual([[1]])
    view.unmount()
    server({ accounts: [] })
    wrapper = mount(UnifiedMailbox)
    await flushPromises()
    expect(wrapper.get('.sync-mail').attributes('disabled')).toBeDefined()
    expect(wrapper.text()).toContain('请先在“邮箱集群”添加发送邮箱')
  })
})
