import { describe, expect, it } from "vitest"
import { hideInternalIds } from "./assistant-text"

describe("older assistant answers without internal ids", () => {
  it("drops labelled ids and raw field values the platform returned", () => {
    expect(hideInternalIds("项目「贪吃蛇」中的任务「收尾自检」（ID: `01M48Y8NP3008Z51ZE6QEH50VZ`）已执行完成。"))
      .toBe("项目「贪吃蛇」中的任务「收尾自检」已执行完成。")
    expect(hideInternalIds("- 执行结果：成功（outcome: succeeded）")).toBe("- 执行结果：成功")
    expect(hideInternalIds("每日简报已为您成功关闭（`enabled: false`），后续不会再发送。"))
      .toBe("每日简报已为您成功关闭，后续不会再发送。")
    expect(hideInternalIds("会话 `session_7YBVQF0QR0ZKR59BAEH1DDDQBH` 已关注")).toBe("会话 已关注")
    expect(hideInternalIds("已提交，任务编号：01M48Y8NP3008Z51ZE6QEH50VZ")).toBe("已提交")
  })

  it("leaves ordinary parentheses, code and words alone", () => {
    const kept = [
      "主色 `#10b981`（荧光翡翠绿）",
      "（注：在父级工作区 `/workspace` 目录下存在文件 `snake.html`）",
      "宽度 (width: 100px) 与比例 16:9",
      "status 这个词本身没关系",
      "用 `npm test` 跑一下",
    ]
    for (const text of kept) expect(hideInternalIds(text)).toBe(text)
  })
})
