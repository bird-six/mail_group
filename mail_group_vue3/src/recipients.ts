export function normalizeCopiedEmail(value: string): string {
  // These characters are clipboard artifacts, not valid address characters.
  return value.replace(/[\u200b\ufeff]/g, '').trim().replace(/^mailto:/i, '').trim().toLowerCase()
}

export function parseRecipients(value: string): { email: string; name: string }[] {
  const lines = value
    .replace(/[\u200b\ufeff]/g, '')
    .replace(/\[([^\]\n]+)\]\(\s*mailto:([^\s)]*)\s*\)/gi, (_match, label: string, target: string) => {
      // Prefer the visible address. A mailto target can list several recipients
      // even when the link label shows just one (as in copied recruitment tables).
      return label.includes('@') ? label : `mailto:${target}`
    })
    .split(/\r?\n/)

  const text = lines.map((line, index) => {
    const trimmed = line.trim()
    const isTableLine = trimmed.startsWith('|') && trimmed.endsWith('|')
    if (!isTableLine) return line
    if (/^[|:\s-]+$/.test(trimmed)) return ''
    const cells = trimmed.replace(/^\|/, '').replace(/\|$/, '').split('|')
    const hasHeaderSeparator = /^[|:\s-]+$/.test(lines[index + 1]?.trim() ?? '')
    if (hasHeaderSeparator && cells.every(cell => /^(邮箱|邮箱地址|收件邮箱|收件人邮箱|email)$/i.test(cell.trim()))) return ''
    return cells.join('\n')
  }).join('\n')
    .replace(/<br\s*\/?>/gi, '\n')
    .replace(/<((?:mailto:)?[^<>\s]+@[^<>\s]+)>/gi, '$1')

  // Keep invalid tokens for server validation; never extract a valid-looking
  // substring from a malformed address or silently drop a recipient.
  const seen = new Set<string>()
  return text.split(/[\s,;，；]+/)
    .map(normalizeCopiedEmail)
    .filter(Boolean)
    .filter(email => {
      if (seen.has(email)) return false
      seen.add(email)
      return true
    })
    .map(email => ({ email, name: '' }))
}
