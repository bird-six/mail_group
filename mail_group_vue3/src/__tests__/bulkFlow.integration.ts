// @vitest-environment jsdom
// Synthetic offline integration: no private fixtures and no real SMTP.
import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process'
import { writeFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { createInterface } from 'node:readline'
import { afterAll, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import CurrentApp from '../App.vue'

const root = resolve(process.cwd(), '..')
const emails = Array.from({ length: 152 }, (_, i) => `audit${i}@example.com`)
type Reply = { status: number; text: string; smtp_calls: number; scheduled_tasks: number; validation_errors?: unknown[] }
type Exchange = Reply & { path: string; method: string; form?: Record<string, string> }

class Backend {
  child: ChildProcessWithoutNullStreams
  pending: { resolve: (value: Reply) => void; reject: (error: Error) => void }[] = []
  stderr = ''
  constructor(version: string) {
    this.child = spawn(process.env.MAIL_GROUP_TEST_PYTHON || resolve(root, '.venv/Scripts/python.exe'), [resolve(root, 'diagnostics/repro_backend.py'), version], { cwd: root, windowsHide: true, env: { ...process.env, PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8' } })
    const lines = createInterface({ input: this.child.stdout })
    lines.on('line', (line) => this.pending.shift()?.resolve(JSON.parse(line)))
    this.child.stderr.on('data', (chunk) => { this.stderr += chunk.toString() })
    this.child.on('exit', (code) => {
      for (const pending of this.pending.splice(0)) pending.reject(new Error(`Backend exited ${code}: ${this.stderr}`))
    })
  }
  call(command: object): Promise<Reply> {
    if (this.child.exitCode !== null) return Promise.reject(new Error(`Backend has exited: ${this.stderr}`))
    return new Promise((resolve, reject) => {
      this.pending.push({ resolve, reject })
      this.child.stdin.write(`${JSON.stringify(command)}\n`)
    })
  }
  close(): Promise<void> {
    if (this.child.exitCode !== null) return Promise.resolve()
    return new Promise((resolve) => {
      this.child.once('exit', () => resolve())
      this.child.stdin.end('{"op":"close"}\n')
    })
  }
}

const reports: Record<string, unknown>[] = []
const changed = (suffix: string, prefix = '') => emails.map((email, i) => i === 8 ? `${prefix}${email}${suffix}` : email).join('\n')
const pairedStarts = new Set([emails[0]!, emails[10]!, emails[20]!])
const tableRows: string[] = []
for (let i = 0; i < emails.length; i++) {
  const pair = pairedStarts.has(emails[i]!) ? [emails[i]!, emails[++i]!] : [emails[i]!]
  tableRows.push(`| ${pair.map(email => `[${email}](mailto:${pair.join(',')})`).join('<br>')} |`)
}
tableRows.splice(1, 0, '| --- |')
type Scenario = { name: string; input: string; count: number; polluted?: boolean; recoverable?: boolean; parseError?: boolean; seed?: string[]; fromDb?: boolean; fixedOnly?: boolean }
const scenarios: Scenario[] = [
  { name: 'clean_first_8', input: emails.slice(0, 8).join('\n'), count: 8 },
  { name: 'clean_150', input: emails.slice(0, 150).join('\n'), count: 150 },
  { name: 'clean_152', input: emails.join('\n'), count: 152 },
  { name: 'zero_width_space_at_9', input: changed('\u200b'), count: 152, polluted: true, recoverable: true },
  { name: 'mailto_at_9', input: changed('', 'mailto:'), count: 152, polluted: true, recoverable: true },
  { name: 'trailing_period_at_9', input: changed('.'), count: 152, polluted: true },
  { name: 'markdown_table', input: emails.map(e => `| [${e}](mailto:${e}) |`).join('\n'), count: 152, parseError: true },
  { name: 'literal_br_between_two_addresses', input: emails.join('\n').replace('audit0@example.com\audit1@example.com', 'audit0@example.com<br>audit1@example.com'), count: 152, parseError: true },
  { name: 'user_table_with_shared_mailto_targets', input: tableRows.join('\n'), count: 152, parseError: true },
  { name: 'legacy_records_with_copy_artifacts', input: '', count: 152, seed: changed('\u200b', 'mailto:').split('\n'), fromDb: true, fixedOnly: true },
  { name: 'failed_import_preserves_existing_8_and_blocks_send', input: changed('.'), count: 152, polluted: true, seed: emails.slice(0, 8), fixedOnly: true },
]

async function navigate(wrapper: VueWrapper, text: string) {
  await wrapper.findAll('nav button').find(button => button.text() === text)!.trigger('click')
}

for (const [version, App] of [['fixed', CurrentApp]] as const) {
  describe.sequential(version, () => {
    const backend = new Backend(version)
    afterAll(() => backend.close())
    for (const scenario of scenarios) {
      if (scenario.fixedOnly && version === 'legacy') continue
      it(scenario.name, async () => {
        await backend.call({ op: 'reset', recipients: scenario.seed })
        const exchanges: Exchange[] = []
        let inflight = 0
        vi.stubGlobal('fetch', async (url: string, options: RequestInit = {}) => {
          const path = new URL(url, 'http://127.0.0.1:8000').pathname + new URL(url, 'http://127.0.0.1:8000').search
          const method = options.method ?? 'GET'
          const command: { path: string; method: string; json?: unknown; form?: Record<string, string> } = { path, method }
          if (typeof options.body === 'string') command.json = JSON.parse(options.body)
          else if (options.body instanceof FormData) command.form = Object.fromEntries(options.body.entries()) as Record<string, string>
          inflight++
          try {
            const reply = await backend.call(command)
            exchanges.push({ ...reply, ...command })
            return { ok: reply.status >= 200 && reply.status < 300, status: reply.status, text: async () => reply.text, json: async () => JSON.parse(reply.text) }
          } finally { inflight-- }
        })
        const settle = async () => {
          await flushPromises()
          await vi.waitFor(() => expect(inflight).toBe(0), { timeout: 5000, interval: 10 })
          await flushPromises()
        }
        const wrapper = mount(App)
        try {
          await settle()
          await navigate(wrapper, '目标邮箱')
          if (!scenario.fromDb) {
            await wrapper.get('.recipient-box').setValue(scenario.input)
            await wrapper.get('.parse-btn').trigger('click')
          }
          await settle()
          const importResult = exchanges.find(e => e.path === '/api/recipients/batch')
          const parseError = wrapper.find('.field-error').exists() ? wrapper.find('.field-error').text() : ''
          const displayedRows = wrapper.findAll('.target-layout .panel:first-child tbody .toggle-switch').length
          const report: Record<string, unknown> = { version, scenario: scenario.name, input_count: scenario.count, import_status: importResult?.status ?? null, parse_error: parseError, displayed_rows: displayedRows }
          if (scenario.parseError && version === 'legacy') {
            expect(importResult).toBeUndefined()
            expect(parseError).not.toBe('')
          } else if (scenario.polluted && !scenario.recoverable && version === 'fixed') {
            expect(importResult?.status).toBe(422)
            expect(parseError).toContain('第9个收件人')
            const remaining = await backend.call({ method: 'GET', path: '/api/recipients' })
            expect(JSON.parse(remaining.text)).toHaveLength(scenario.seed?.length ?? 0)
            await navigate(wrapper, '邮件内容')
            await wrapper.get('.saved-mail-item').trigger('click')
            await navigate(wrapper, '首页')
            expect(wrapper.findAll('button').find(button => button.text() === '开始群发')!.attributes('disabled')).toBeDefined()
          } else {
            if (!scenario.fromDb) expect(importResult?.status).toBe(200)
            expect(displayedRows).toBe(8)
            await navigate(wrapper, '邮件内容')
            await wrapper.get('.saved-mail-item').trigger('click')
            await navigate(wrapper, '首页')
            const send = wrapper.findAll('button').find(button => button.text() === '开始群发')!
            expect(send.attributes('disabled')).toBeUndefined()
            await send.trigger('click')
            await settle()
            const taskResult = exchanges.find(e => e.path === '/api/tasks' && e.method === 'POST')!
            const payload = JSON.parse(taskResult.form!.payload!)
            expect(payload.recipients).toHaveLength(scenario.count)
            Object.assign(report, { task_status: taskResult.status, payload_recipient_count: payload.recipients.length, notice: wrapper.find('.notice').exists() ? wrapper.find('.notice').text() : '', validation_errors: taskResult.validation_errors ?? [] })
            if (scenario.polluted && version === 'legacy') {
              expect(taskResult.status).toBe(400)
              expect(JSON.parse(taskResult.text).detail).toBe('任务参数格式错误')
              // Keep the template, sender and stored addresses unchanged; disable only addresses after the first eight.
              const imported = JSON.parse(importResult!.text)
              for (const recipient of imported.slice(8)) await backend.call({ path: `/api/recipients/${recipient.id}/toggle`, method: 'PATCH' })
              wrapper.unmount()
              const retry = mount(App)
              try {
                await settle()
                await navigate(retry, '邮件内容')
                await retry.get('.saved-mail-item').trigger('click')
                await navigate(retry, '首页')
                await retry.findAll('button').find(button => button.text() === '开始群发')!.trigger('click')
                await settle()
                const retryResult = exchanges.filter(e => e.path === '/api/tasks' && e.method === 'POST').at(-1)!
                expect(retryResult.status).toBe(200)
                expect(JSON.parse(retryResult.text).total).toBe(8)
                report.first_8_retry_status = retryResult.status
                report.first_8_retry_count = JSON.parse(retryResult.text).total
              } finally { retry.unmount() }
            } else {
              expect(taskResult.status).toBe(200)
              expect(JSON.parse(taskResult.text).total).toBe(scenario.count)
              expect(new Set(JSON.parse(taskResult.text).assignments.map((item: {recipient: string}) => item.recipient)))
                .toEqual(new Set(emails.slice(0, scenario.count).map(email => email.toLowerCase())))
            }
          }
          expect(exchanges.every(e => e.smtp_calls === 0)).toBe(true)
          report.smtp_calls = 0
          reports.push(report)
        } finally {
          wrapper.unmount()
          vi.unstubAllGlobals()
        }
      }, 30000)
    }
  })
}

afterAll(() => {
  writeFileSync(resolve(root, 'diagnostics/functional-audit-integration.json'), JSON.stringify(reports, null, 2))
})
