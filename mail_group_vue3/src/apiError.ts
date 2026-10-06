interface ValidationIssue {
  loc?: (string | number)[]
  msg?: string
  type?: string
  input?: unknown
}

const fieldLabels: Record<string, string> = {
  content: '邮件内容',
  subject: '主题',
  body: '正文',
  email: '邮箱',
  auth_code: '授权码',
  smtp_host: 'SMTP 服务器',
  smtp_port: 'SMTP 端口',
  senders: '发送邮箱',
  recipients: '收件人',
}

function formatValidationIssue(issue: ValidationIssue): string {
  const location = [...(issue.loc ?? [])]
  if (location[0] === 'body') location.shift()
  let prefix = ''
  if (typeof location[0] === 'number') {
    const index = location.shift() as number
    prefix = `第${index + 1}个收件人`
  } else if (['recipients', 'senders'].includes(String(location[0])) && typeof location[1] === 'number') {
    const group = location.shift() as string
    const index = location.shift() as number
    prefix = `第${index + 1}个${fieldLabels[group]}`
  }
  const field = location.at(-1)
  const label = location.map((part) => fieldLabels[String(part)] ?? String(part)).join(' / ')
  let message = issue.msg?.replace(/^Value error, /, '') || '参数无效'
  if (field === 'email') {
    message = '邮箱格式无效，请检查多余符号、连续句点或域名'
    if (issue.msg?.includes('cannot end with a period')) message = '邮箱末尾不能有句点，请核对原始地址'
    else if (issue.msg?.includes('two periods in a row')) message = '邮箱不能包含连续句点'
    else if (issue.msg?.includes('unsafe characters')) message = '邮箱含不可见或不安全字符，请删除标出的字符'
  }
  else if (field === 'subject' && issue.type === 'string_too_long') message = '主题不能超过200个字符'
  else if (issue.type === 'missing' || issue.type === 'string_too_short') message = '不能为空'
  else if (issue.type === 'json_invalid') message = '任务数据格式无效，请刷新页面后重试'
  const displayedInput = typeof issue.input === 'string'
    ? issue.input.replace(/[\p{Cf}\p{Cc}]/gu, character => `[U+${character.codePointAt(0)!.toString(16).toUpperCase().padStart(4, '0')}]`)
    : ''
  const email = field === 'email' && typeof issue.input === 'string' ? `（${displayedInput}）` : ''
  return `${[prefix, label].filter(Boolean).join(' / ')}${email}：${message}`
}

export function formatApiError(text: string, status: number): string {
  try {
    const data = JSON.parse(text) as { detail?: string | ValidationIssue[] }
    if (typeof data.detail === 'string') return data.detail
    if (Array.isArray(data.detail)) {
      const messages = data.detail.slice(0, 10).map(formatValidationIssue)
      if (data.detail.length > 10) messages.push(`另有${data.detail.length - 10}项错误，请修正后重新提交`)
      return messages.join('；')
    }
  } catch { /* 非 JSON 错误保留原始说明 */ }
  return text || `请求失败：${status}`
}
