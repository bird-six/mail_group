import { describe, expect, it } from 'vitest'
import { normalizeCopiedEmail, parseRecipients } from '../recipients'

const addresses = (text: string) => parseRecipients(text).map(item => item.email)

describe('收件人复制格式处理', () => {
  it('处理表格、重复链接目标和同一格的两个邮箱', () => {
    const text = '| 邮箱 |\n| --- |\n| [A@example.com](mailto:a@example.com,b@example.com)<br>[b@example.com](mailto:a@example.com,b@example.com) |\n| [C@example.com](mailto:c@example.com) |'
    expect(addresses(text)).toEqual(['a@example.com', 'b@example.com', 'c@example.com'])
  })

  it('保留作为首行表头的真实邮箱', () => {
    expect(addresses('| [first@example.com](mailto:first@example.com) |\n| ---- |\n| second@example.com |'))
      .toEqual(['first@example.com', 'second@example.com'])
  })

  it('按清理后的地址去重，兼容零宽空格、BOM、mailto及中英文分隔符', () => {
    expect(addresses('\ufeffMAILTO:A@EXAMPLE.COM\u200b，a@example.com；b@example.com,\nc@example.com;d@example.com'))
      .toEqual(['a@example.com', 'b@example.com', 'c@example.com', 'd@example.com'])
  })

  it('处理换行标签和尖括号地址', () => {
    expect(addresses('<mailto:a@example.com><BR /><b@example.com>')).toEqual(['a@example.com', 'b@example.com'])
  })

  it('使用链接中明确显示的邮箱，不引入隐藏目标或抄送地址', () => {
    expect(addresses('[shown@example.com](mailto:hidden@example.com?cc=other@example.com)')).toEqual(['shown@example.com'])
  })

  it('保留异常条目交给服务端拒绝，不截取子串、不跳过无效地址', () => {
    expect(addresses('good@example.com\nbad..address@example.com\nuser@example.com.\ninvalid'))
      .toEqual(['good@example.com', 'bad..address@example.com', 'user@example.com.', 'invalid'])
  })

  it('保留有效本地部分的竖线和加号', () => {
    expect(addresses('|sales+jobs@example.com')).toEqual(['|sales+jobs@example.com'])
  })

  it('单条输入使用同样的复制清理规则', () => {
    expect(normalizeCopiedEmail(' MAILTO:\u200bUser@Example.com\ufeff ')).toBe('user@example.com')
    expect(normalizeCopiedEmail('user@example.com.')).toBe('user@example.com.')
  })
})
