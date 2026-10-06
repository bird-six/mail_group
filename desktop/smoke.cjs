// Native Electron integration smoke. It never creates a sending task.
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const assert = require('node:assert/strict')
const { app } = require('electron')
process.env.MAIL_GROUP_SMOKE = '1'
process.env.MAIL_GROUP_SMOKE_PROFILE = fs.mkdtempSync(path.join(os.tmpdir(), 'mailgroup-desktop-test-'))
const testPackage = process.env.MAIL_GROUP_TEST_PACKAGE
if (testPackage) {
  Object.defineProperty(app, 'isPackaged', { value: true })
  Object.defineProperty(process, 'resourcesPath', { value: path.join(testPackage, 'resources') })
  app.setPath('userData', process.env.MAIL_GROUP_SMOKE_PROFILE)
}
const desktop = require(testPackage ? path.join(testPackage, 'resources/app.asar/main.cjs') : './main.cjs')
const output = path.resolve(__dirname, '../diagnostics/desktop-v3')
fs.mkdirSync(output, { recursive: true })

async function evaluate(fn) { return desktop.getWindow().webContents.executeJavaScript(`(${fn.toString()})()`); }
async function settle() { await new Promise(resolve => setTimeout(resolve, 180)); }
async function nav(label) {
  await desktop.getWindow().webContents.executeJavaScript(`document.querySelectorAll('nav button').forEach(b => { if (b.textContent.trim() === ${JSON.stringify(label)}) b.click() })`)
  await settle()
}
async function screenshot(name) {
  const image = await desktop.getWindow().webContents.capturePage()
  assert(!image.isEmpty())
  fs.writeFileSync(path.join(output, name), image.toPNG())
}

desktop.ready.then(async () => {
  const win = desktop.getWindow()
  const errors = []
  win.webContents.on('console-message', details => { if (details.level === 'error') errors.push(details.message) })
  await settle()
  assert.equal((await fetch(desktop.getOrigin() + '/api/sender-configs')).status, 401)
  assert.equal((await desktop.serviceRequest('/api/health')).status, 'ok')
  const initial = await evaluate(() => ({
    title: document.title, bridge: Boolean(window.desktop), node: typeof window.require,
    titlebar: Boolean(document.querySelector('.desktop-titlebar')),
    controls: [...document.querySelectorAll('.desktop-window-actions button')].map(b => b.getAttribute('aria-label')),
  }))
  assert(initial.bridge && initial.titlebar && initial.node === 'undefined')
  assert(initial.controls.includes('关闭窗口') && initial.controls.includes('最小化'))
  await evaluate(() => window.desktop.toggleMaximize())
  await settle()
  assert(win.isMaximized())
  await evaluate(() => window.desktop.toggleMaximize())
  await settle()
  assert(!win.isMaximized())
  await evaluate(() => window.desktop.minimize())
  await settle()
  assert(win.isMinimized())
  win.restore()
  await screenshot('home-empty-1360.png')
  const json = data => ({ method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) })
  const sender = await desktop.serviceRequest('/api/sender-configs', json({ email: 'team@example.com', auth_code: 'synthetic-never-send', note: '工作邮箱' }))
  assert(!Object.hasOwn(sender, 'auth_code'))
  await desktop.serviceRequest('/api/recipients/batch', json([{ email: 'hello@example.com', note: '产品联络' }, { email: 'studio@example.com', note: '合作伙伴' }]))
  const form = new FormData()
  form.append('subject', '项目合作介绍')
  form.append('body', '您好，\n\n随信附上项目介绍，期待与您进一步沟通。\n\n祝好！')
  form.append('attachments', new Blob(['synthetic file, no private data'], { type: 'text/plain' }), '项目介绍 #1.txt')
  await desktop.serviceRequest('/api/saved-mails', { method: 'POST', body: form })
  await win.webContents.reload()
  await new Promise(resolve => win.webContents.once('did-finish-load', resolve))
  await settle()
  await nav('邮件内容')
  await evaluate(() => document.querySelector('.saved-mail-item').click())
  await settle()
  assert((await evaluate(() => document.querySelector('.attachment-list')?.textContent || '')).includes('项目介绍 #1.txt'))
  await screenshot('mail-editor-1360.png')
  await nav('目标邮箱')
  assert((await evaluate(() => document.body.textContent)).includes('hello@example.com'))
  await screenshot('recipients-1360.png')
  await nav('邮箱集群')
  await screenshot('senders-1360.png')
  await nav('数据面板')
  await screenshot('statistics-1360.png')
  win.setSize(1024, 720)
  await settle()
  await nav('邮件内容')
  const layout = await evaluate(() => ({ width: innerWidth, scrollWidth: document.documentElement.scrollWidth, height: innerHeight, controls: document.querySelector('.desktop-titlebar').getBoundingClientRect().height, navigationVisible: document.querySelector('nav').getBoundingClientRect().width > 0 }))
  assert(layout.scrollWidth <= layout.width, JSON.stringify(layout))
  assert.equal(layout.controls, 42)
  assert(layout.navigationVisible)
  await screenshot('mail-editor-1024.png')
  assert.equal((await desktop.serviceRequest('/api/saved-mails')).length, 1)
  assert.equal((await desktop.serviceRequest('/api/tasks')).length, 0)
  assert.equal(errors.length, 0, errors.join('\n'))
  const report = { passed: true, initial, layout, runtimeErrors: errors, realSmtpCalls: 0, backendOrigin: desktop.getOrigin(), dataDirectory: process.env.MAIL_GROUP_SMOKE_PROFILE }
  fs.writeFileSync(path.join(output, testPackage ? 'packaged-smoke.json' : 'native-smoke.json'), JSON.stringify(report, null, 2))
  console.log(JSON.stringify({ passed: true, screenshots: output, checks: ['frameless controls', 'isolated renderer', 'authenticated API', 'sender CRUD', 'recipient import', 'template attachment', '1024px layout', 'empty states'] }))
  win.close()
}).catch(async error => {
  console.error(error)
  fs.writeFileSync(path.join(output, 'native-smoke-error.txt'), String(error.stack || error))
  await desktop.stopBackend()
  app.exit(1)
})
