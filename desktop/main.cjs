const { app, BrowserWindow, ipcMain, dialog, shell } = require('electron')
const { spawn } = require('node:child_process')
const { randomBytes } = require('node:crypto')
const fs = require('node:fs')
const path = require('node:path')
const readline = require('node:readline')
const { pathToFileURL } = require('node:url')

app.setName('MailGroup')
// Standard explicit profile override, also useful for isolated portable workspaces.
const profileDirectory = app.commandLine.getSwitchValue('user-data-dir')
if (profileDirectory) app.setPath('userData', path.resolve(profileDirectory))
const isSmoke = process.env.MAIL_GROUP_SMOKE === '1' && !app.isPackaged
if (isSmoke && process.env.MAIL_GROUP_SMOKE_PROFILE) app.setPath('userData', process.env.MAIL_GROUP_SMOKE_PROFILE)
const haveLock = app.requestSingleInstanceLock()
if (!haveLock) app.quit()

let mainWindow
let backend
let backendOrigin = ''
const token = randomBytes(32).toString('base64url')
let quitting = false
let closeInProgress = false
const dataDirectory = path.join(app.getPath('userData'), 'data')
const loadingPath = path.join(__dirname, 'loading.html')

function trustedFrame(event) {
  if (event.sender !== mainWindow?.webContents || event.senderFrame !== mainWindow.webContents.mainFrame) return false
  const url = new URL(event.senderFrame.url)
  return url.origin === backendOrigin || url.href.split('?')[0] === pathToFileURL(loadingPath).href
}

function registerWindowControls() {
  const commands = {
    'window:minimize': () => mainWindow.minimize(),
    'window:maximize': () => mainWindow.isMaximized() ? mainWindow.unmaximize() : mainWindow.maximize(),
    'window:close': () => mainWindow.close(),
    'window:state': () => ({ maximized: mainWindow.isMaximized() }),
    'window:data-folder': () => shell.openPath(dataDirectory),
  }
  for (const [channel, action] of Object.entries(commands)) {
    ipcMain.handle(channel, event => {
      if (!trustedFrame(event)) throw new Error('Untrusted window')
      return action()
    })
  }
}

async function serviceRequest(route, options = {}) {
  const response = await fetch(backendOrigin + route, {
    ...options, headers: { Authorization: `Bearer ${token}`, ...options.headers }, signal: AbortSignal.timeout(3000),
  })
  if (!response.ok) throw new Error(`Service response ${response.status}`)
  return response.json()
}

async function stopBackend() {
  if (!backend || backend.exitCode !== null) return
  try { await serviceRequest('/api/shutdown', { method: 'POST' }) } catch { /* May already be stopped. */ }
  await new Promise(resolve => {
    const timer = setTimeout(() => { backend.kill(); resolve() }, 45000)
    backend.once('exit', () => { clearTimeout(timer); resolve() })
    if (backend.exitCode !== null) { clearTimeout(timer); resolve() }
  })
}

async function requestClose(event) {
  if (quitting) return
  event.preventDefault()
  if (closeInProgress) return
  closeInProgress = true
  try {
    let tasks = []
    try { tasks = await serviceRequest('/api/tasks') } catch { /* Closing remains possible after a crash. */ }
    const active = tasks.filter(task => ['pending', 'running'].includes(task.status))
    if (active.length && !isSmoke) {
      const { response } = await dialog.showMessageBox(mainWindow, {
        type: 'question', title: '还有邮件正在发送', message: '是否暂停任务并退出？',
        detail: '正在发送的邮件会等待结果，其余邮件保留在任务列表，下次打开可继续发送。',
        buttons: ['继续发送', '暂停并退出'], defaultId: 0, cancelId: 0,
      })
      if (response === 0) return
    }
    await mainWindow.loadFile(loadingPath, { query: { state: 'closing' } })
    await stopBackend()
    quitting = true
    app.quit()
  } finally { closeInProgress = false }
}

async function startBackend() {
  fs.mkdirSync(dataDirectory, { recursive: true })
  const executable = app.isPackaged
    ? path.join(process.resourcesPath, 'backend', 'mail-group-service.exe')
    : path.resolve(__dirname, '../.venv/Scripts/python.exe')
  const args = app.isPackaged ? [] : ['-B', path.resolve(__dirname, '../backend/main.py')]
  args.push('--desktop', '--parent-pid', String(process.pid))
  const childEnvironment = { ...process.env, MAIL_GROUP_API_TOKEN: token, MAIL_GROUP_DATA_DIR: dataDirectory, PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8' }
  // Portable Python's frozen runtime must not inherit another Python installation.
  delete childEnvironment.PYTHONHOME
  delete childEnvironment.PYTHONPATH
  delete childEnvironment.MAIL_GROUP_DEV_ORIGIN
  backend = spawn(executable, args, { windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'], env: childEnvironment })
  let errorTail = ''
  backend.stderr.on('data', chunk => { errorTail = (errorTail + chunk.toString()).slice(-5000) })
  const port = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('本地服务启动超时')), 30000)
    const lines = readline.createInterface({ input: backend.stdout })
    lines.on('line', line => {
      try {
        const message = JSON.parse(line)
        if (message.event === 'ready' && Number.isInteger(message.port) && message.port > 0 && message.port < 65536) {
          clearTimeout(timer); resolve(message.port)
        }
      } catch { /* Ignore ordinary service messages. */ }
    })
    backend.once('error', error => { clearTimeout(timer); reject(error) })
    backend.once('exit', code => { clearTimeout(timer); reject(new Error(`本地服务未能启动（${code}）\n${errorTail}`)) })
  })
  backendOrigin = `http://127.0.0.1:${port}`
  for (let attempt = 0; attempt < 100; attempt++) {
    if (backend.exitCode !== null) throw new Error('本地服务已退出')
    try { await serviceRequest('/api/health'); return } catch { await new Promise(resolve => setTimeout(resolve, 100)) }
  }
  throw new Error('本地服务未就绪，请查看数据文件夹中的 server.log')
}

async function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1360, height: 900, minWidth: 1024, minHeight: 720, show: false,
    frame: false, autoHideMenuBar: true, backgroundColor: '#f4f6f8',
    icon: app.isPackaged ? path.join(process.resourcesPath, 'backend', '_internal', 'mail.ico') : path.resolve(__dirname, '../release/mail.ico'),
    webPreferences: { preload: path.join(__dirname, 'preload.cjs'), contextIsolation: true, nodeIntegration: false, sandbox: true, webSecurity: true, devTools: !app.isPackaged },
  })
  mainWindow.setMenu(null)
  mainWindow.on('close', requestClose)
  const windowSession = mainWindow.webContents.session
  windowSession.setPermissionRequestHandler((_webContents, _permission, callback) => callback(false))
  windowSession.setPermissionCheckHandler(() => false)
  windowSession.webRequest.onBeforeSendHeaders((details, callback) => {
    const headers = { ...details.requestHeaders }
    // Never send a renderer-provided or cached credential to a different origin.
    if (details.webContentsId === mainWindow?.webContents.id && new URL(details.url).origin === backendOrigin) {
      headers.Authorization = `Bearer ${token}`
    }
    callback({ requestHeaders: headers })
  })
  mainWindow.webContents.setWindowOpenHandler(() => ({ action: 'deny' }))
  mainWindow.webContents.on('will-navigate', (event, url) => {
    if (new URL(url).origin !== backendOrigin) event.preventDefault()
  })
  mainWindow.webContents.on('will-attach-webview', event => event.preventDefault())
  mainWindow.on('maximize', () => mainWindow.webContents.send('window:state', { maximized: true }))
  mainWindow.on('unmaximize', () => mainWindow.webContents.send('window:state', { maximized: false }))
  await mainWindow.loadFile(loadingPath)
  if (!isSmoke) mainWindow.show()
  await startBackend()
  await mainWindow.loadURL(backendOrigin)
  if (!isSmoke) mainWindow.show()
  backend.once('exit', () => {
    if (!quitting && !closeInProgress && !mainWindow.isDestroyed()) {
      void mainWindow.loadFile(loadingPath, { query: { state: 'error' } })
      if (!isSmoke) dialog.showErrorBox('本地服务已停止', '请关闭并重新打开应用。已保存的内容仍在数据文件夹中，未完成任务可在重启后恢复。')
    }
  })
}

app.on('second-instance', () => { if (mainWindow) { if (mainWindow.isMinimized()) mainWindow.restore(); mainWindow.show(); mainWindow.focus() } })
app.on('window-all-closed', () => { if (quitting) app.quit() })
app.on('before-quit', event => { if (!quitting && mainWindow && !mainWindow.isDestroyed()) { event.preventDefault(); mainWindow.close() } })

const ready = haveLock ? app.whenReady().then(async () => {
  registerWindowControls()
  try { await createWindow() } catch (error) {
    if (!isSmoke) dialog.showErrorBox('应用启动失败', `${error.message}\n\n数据文件夹：${dataDirectory}`)
    await stopBackend()
    quitting = true
    app.quit()
    throw error
  }
}) : Promise.resolve()

// Main-process exports support native integration tests; none are exposed to the renderer.
module.exports = { ready, getWindow: () => mainWindow, getOrigin: () => backendOrigin, getBackend: () => backend, serviceRequest, stopBackend }
