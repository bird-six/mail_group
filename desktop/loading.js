const state = new URLSearchParams(location.search).get('state')
const heading = document.querySelector('h1')
const message = document.querySelector('.message')
const progress = document.querySelector('.progress')
if (state === 'closing') {
  heading.textContent = '正在保存并退出'
  message.textContent = '等待当前邮件返回结果，剩余任务会保留，下次打开可继续。'
  progress.setAttribute('aria-label', '正在保存')
} else if (state === 'error') {
  heading.textContent = '本地服务已停止'
  message.textContent = '请关闭并重新打开应用。已保存的内容仍在数据文件夹中。'
  progress.hidden = true
}
document.querySelector('[data-action="minimize"]').addEventListener('click', () => window.desktop.minimize())
document.querySelector('[data-action="close"]').addEventListener('click', () => window.desktop.close())
