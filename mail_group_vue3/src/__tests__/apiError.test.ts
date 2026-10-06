import { describe, expect, it } from 'vitest'
import { formatApiError } from '../apiError'

describe('批量收件人错误提示', () => {
  it.each([
    ['body', 149, 'email'],
    ['recipients', 149, 'email'],
  ])('显示导入或群发失败的收件人序号和地址 (%j)', (...loc) => {
    const text = JSON.stringify({ detail: [{ loc, msg: 'value is not a valid email address', input: 'bad..address@example.com' }] })
    const result = formatApiError(text, 400)
    expect(result).toContain('第150个收件人')
    expect(result).toContain('bad..address@example.com')
    expect(result).toContain('邮箱格式无效')
    expect(result).not.toContain('"detail"')
  })

  it('指出模板主题过长', () => {
    expect(formatApiError(JSON.stringify({ detail: [{ loc: ['content', 'subject'], type: 'string_too_long' }] }), 400))
      .toContain('主题不能超过200个字符')
  })

  it('不在发送邮箱校验错误中显示授权码', () => {
    const result = formatApiError(JSON.stringify({ detail: [{ loc: ['senders', 0, 'auth_code'], type: 'string_too_short', input: 'secret' }] }), 422)
    expect(result).toContain('第1个发送邮箱 / 授权码：不能为空')
    expect(result).not.toContain('secret')
  })

  it('保留业务错误和非JSON错误说明', () => {
    expect(formatApiError('{"detail":"附件超过50MB限制"}', 400)).toBe('附件超过50MB限制')
    expect(formatApiError('Internal Server Error', 500)).toBe('Internal Server Error')
    expect(formatApiError('', 502)).toBe('请求失败：502')
  })

  it('限制长批次的错误提示长度', () => {
    const detail = Array.from({ length: 150 }, (_, i) => ({ loc: ['recipients', i, 'email'], input: `bad${i}` }))
    const result = formatApiError(JSON.stringify({ detail }), 400)
    expect(result).toContain('第10个收件人')
    expect(result).not.toContain('第11个收件人')
    expect(result).toContain('另有140项错误')
  })

  it('显示无法清理的不可见字符的编码和具体原因', () => {
    const result = formatApiError(JSON.stringify({ detail: [{ loc: ['recipients', 8, 'email'], input: 'hr\u2060@example.com', msg: 'The email address contains unsafe characters: WORD JOINER.' }] }), 400)
    expect(result).toContain('第9个收件人')
    expect(result).toContain('[U+2060]')
    expect(result).toContain('不可见')
  })
})
