// Regression controls for the repaired offline UI workflows.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import App from '../App.vue'

type Mail = { id: number; subject: string; body: string; created_at: string; attachments: string[] }
const mails: Mail[] = [
  { id: 1, subject: 'Template A', body: 'Body A', created_at: '2026-10-06T10:00:00', attachments: ['uuid_A.txt'] },
  { id: 2, subject: 'Template B', body: 'Body B', created_at: '2026-10-06T10:00:00', attachments: [] },
]
const blankStats = { stats: [], total_success: 0, total_failed: 0, success_rate: 0, total_prev: 0, success_prev: 0, failed_prev: 0, rate_prev: 0 }
let wrapper: VueWrapper | undefined

function response(data: unknown, status = 200) {
  return { ok: status >= 200 && status < 300, status, json: async () => JSON.parse(JSON.stringify(data)), text: async () => JSON.stringify(data) }
}

function mockServer(attachmentFetch?: () => Promise<unknown>) {
  const recipient = { id: 1, email: 'recipient@example.com', enabled: true, note: '', created_at: '2026-10-06T10:00:00' }
  const stored = [recipient]
  const requests: { path: string; method: string; body?: RequestInit['body'] }[] = []
  const submitted: { subject: string; recipients: string[]; files: string[] }[] = []
  const fetch = vi.fn(async (url: string, options: RequestInit = {}) => {
    const path = new URL(url, 'http://127.0.0.1:8000').pathname
    const method = options.method ?? 'GET'
    requests.push({ path, method, body: options.body })
    if (path.includes('/attachments/')) return attachmentFetch ? attachmentFetch() : response({}, 404)
    if (path === '/api/sender-configs') return response([{ id: 1, email: 'sender@example.com', auth_code: 'audit-placeholder', smtp_host: 'smtp.example.com', smtp_port: 465, note: '', enabled: true, created_at: '2026-10-06T10:00:00' }])
    if (path === '/api/recipients') return response(stored)
    if (path === '/api/recipients/batch') {
      const submittedRecipients = JSON.parse(options.body as string) as { email: string }[]
      const added = submittedRecipients.map((r, i) => ({ ...recipient, id: i + 2, email: r.email }))
      stored.push(...added)
      return response(added)
    }
    if (path === '/api/saved-mails') return response(mails)
    if (path === '/api/stats') return response(blankStats)
    if (path === '/api/stats/failed-recipients/total') return response({ total: 0 })
    if (path.startsWith('/api/stats/')) return response([])
    if (path === '/api/tasks' && method === 'POST') {
      const form = options.body as FormData
      const payload = JSON.parse(form.get('payload') as string)
      submitted.push({ subject: payload.content?.subject, recipients: payload.recipients.map((r: { email: string }) => r.email), files: form.getAll('attachments').map(f => (f as File).name) })
      recipient.enabled = false // Matches the real worker's success behavior.
      return response({ task_id: `audit-${submitted.length}`, status: 'completed', total: 1, success: 1, failed: 0, pending: 0, progress: 100, created_at: '2026-10-06T10:00:00', updated_at: '2026-10-06T10:00:00', assignments: [] })
    }
    if (path === '/api/tasks') return response([])
    throw new Error(`Unexpected audit request: ${method} ${path}`)
  })
  vi.stubGlobal('fetch', fetch)
  return { recipient, requests, submitted }
}

async function navigate(label: string) {
  await wrapper!.findAll('nav button').find(button => button.text() === label)!.trigger('click')
  await flushPromises()
}

async function selectTemplate(index: number) {
  await navigate('邮件内容')
  await wrapper!.findAll('.saved-mail-item')[index]!.get('.edit-template').trigger('click')
  await flushPromises()
}

async function send() {
  await navigate('首页')
  const button = wrapper!.findAll('button').find(button => button.text() === '开始群发')!
  expect(button.attributes('disabled')).toBeUndefined()
  await button.trigger('click')
  await flushPromises()
}

beforeEach(() => vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] }))
afterEach(() => {
  wrapper?.unmount()
  wrapper = undefined
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('Repaired desktop UI workflows', () => {
  it('sends a multi-selected pool without requiring editor content or mixing editor attachments', async () => {
    const server = mockServer(() => Promise.resolve({ ok: true, status: 200, blob: async () => new Blob(['A'], { type: 'text/plain' }) }))
    wrapper = mount(App)
    await flushPromises()
    await selectTemplate(0)
    expect(wrapper.find('.attachment-list').text()).toContain('A.txt')
    for (const row of wrapper.findAll('.saved-mail-item')) await row.trigger('click')
    expect(wrapper.findAll('.saved-mail-checkbox').every(box => (box.element as HTMLInputElement).checked)).toBe(true)
    expect(wrapper.get('.mail-editor-fields').attributes('disabled')).toBeDefined()
    await send()
    const form = server.requests.find(r => r.path === '/api/tasks' && r.method === 'POST')!.body as FormData
    const payload = JSON.parse(form.get('payload') as string)
    expect(payload.template_ids).toEqual([1, 2])
    expect(payload.content).toBeUndefined()
    expect(form.getAll('attachments')).toEqual([])
  })

  it('supports select all, deselect and empty selection without falling back to the editor', async () => {
    mockServer()
    wrapper = mount(App)
    await flushPromises()
    await selectTemplate(1)
    await wrapper.findAll('button').find(b => b.text() === '全选')!.trigger('click')
    expect(wrapper.findAll('.saved-mail-checkbox').every(box => (box.element as HTMLInputElement).checked)).toBe(true)
    await wrapper.findAll('.saved-mail-item')[0]!.trigger('click')
    expect((wrapper.findAll('.saved-mail-checkbox')[0]!.element as HTMLInputElement).checked).toBe(false)
    await wrapper.findAll('button').find(b => b.text() === '清空选择')!.trigger('click')
    await navigate('首页')
    expect(wrapper.findAll('button').find(b => b.text() === '开始群发')!.attributes('disabled')).toBeDefined()
    expect(wrapper.get('.send-source-summary').text()).toContain('已选 0 个')
    await navigate('邮件内容')
    await wrapper.get('input[name="send-mode"][value="current"]').setValue()
    await navigate('首页')
    expect(wrapper.findAll('button').find(b => b.text() === '开始群发')!.attributes('disabled')).toBeUndefined()
  })

  it('can send selected templates from a blank editor', async () => {
    const server = mockServer()
    wrapper = mount(App)
    await flushPromises()
    await navigate('邮件内容')
    await wrapper.findAll('.saved-mail-checkbox')[1]!.setValue(true)
    await send()
    const form = server.requests.find(r => r.path === '/api/tasks' && r.method === 'POST')!.body as FormData
    expect(JSON.parse(form.get('payload') as string).template_ids).toEqual([2])
  })

  it('removes deleted templates from the selected pool', async () => {
    vi.stubGlobal('confirm', () => true)
    const server = mockServer()
    const fetch = globalThis.fetch
    vi.stubGlobal('fetch', (url: string, options?: RequestInit) => options?.method === 'DELETE'
      ? Promise.resolve(response({ status: 'ok' })) : fetch(url, options))
    wrapper = mount(App)
    await flushPromises()
    await navigate('邮件内容')
    await wrapper.findAll('.saved-mail-item')[1]!.trigger('click')
    await wrapper.findAll('.saved-mail-item')[1]!.get('.delete-btn').trigger('click')
    await flushPromises()
    await navigate('首页')
    expect(wrapper.get('.send-source-summary').text()).toContain('已选 0 个')
    expect(wrapper.findAll('button').find(b => b.text() === '开始群发')!.attributes('disabled')).toBeDefined()
    expect(server.submitted).toHaveLength(0)
  })

  it('synchronizes successful recipients and blocks another send', async () => {
    const server = mockServer()
    wrapper = mount(App)
    await flushPromises()
    await selectTemplate(1)
    await send()
    expect(server.recipient.enabled).toBe(false)
    vi.advanceTimersByTime(6000)
    await flushPromises()
    await navigate('首页')
    expect(wrapper.findAll('button').find(b => b.text() === '开始群发')!.attributes('disabled')).toBeDefined()
    expect(server.submitted).toHaveLength(1)
    expect(server.requests.filter(x => x.path === '/api/recipients').length).toBeGreaterThan(1)
  })

  it('reports missing template attachments and prevents sending', async () => {
    const server = mockServer()
    wrapper = mount(App)
    await flushPromises()
    await selectTemplate(0)
    expect(wrapper.find('.field-error').text()).toContain('已阻止发送')
    await navigate('首页')
    expect(wrapper.findAll('button').find(b => b.text() === '开始群发')!.attributes('disabled')).toBeDefined()
    expect(server.submitted).toHaveLength(0)
  })

  it('blocks sending until all template attachments finish loading', async () => {
    let finish!: (value: unknown) => void
    const download = new Promise(resolve => { finish = resolve })
    const server = mockServer(() => download)
    wrapper = mount(App)
    await flushPromises()
    await selectTemplate(0)
    await navigate('首页')
    expect(wrapper.findAll('button').find(b => b.text() === '开始群发')!.attributes('disabled')).toBeDefined()
    expect(server.submitted).toHaveLength(0)
    finish(response({}, 404))
    await flushPromises()
  })

  it('discards a stale template attachment after switching templates', async () => {
    let finish!: (value: unknown) => void
    const download = new Promise(resolve => { finish = resolve })
    const server = mockServer(() => download)
    wrapper = mount(App)
    await flushPromises()
    await selectTemplate(0)
    await selectTemplate(1)
    finish({ ok: true, status: 200, blob: async () => new Blob(['Template A bytes'], { type: 'text/plain' }) })
    await flushPromises()
    await send()
    expect(server.submitted[0]).toEqual({ subject: 'Template B', recipients: ['recipient@example.com'], files: [] })
  })

  it('preserves existing recipients after an additional import', async () => {
    mockServer()
    wrapper = mount(App)
    await flushPromises()
    await navigate('目标邮箱')
    expect(wrapper.text()).toContain('recipient@example.com')
    await wrapper.get('.recipient-box').setValue('new@example.com')
    await wrapper.get('.parse-btn').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('new@example.com')
    expect(wrapper.text()).toContain('recipient@example.com')
  })
})
