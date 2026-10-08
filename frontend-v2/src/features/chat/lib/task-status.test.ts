import { describe, expect, it } from "vitest"
import { isActiveTask, plainSummary, sinceLabel, taskStatus } from "./task-status"

describe("task status in plain words", () => {
  it.each([
    [{ pendingQuestions: 1, sessionStatus: "busy" }, "waiting"],
    [{ sessionStatus: "waiting_input" }, "waiting"],
    [{ sessionStatus: "busy", observedState: "idle" }, "running"],
    [{ observedState: "running" }, "running"],
    [{ sessionStatus: "queued" }, "queued"],
    [{ desiredState: "paused", observedState: "running", sessionStatus: "idle" }, "paused"],
    [{ desiredState: "canceled", observedState: "running" }, "stopped"],
    [{ observedState: "effect_unknown" }, "failed"],
    [{ sessionStatus: "error" }, "failed"],
    [{ observedState: "completed" }, "done"],
    [{ observedState: "completed", outcome: "error" }, "failed"],
    [{ observedState: "idle", outcome: "succeeded" }, "done"],
    [{ observedState: "idle", outcome: "aborted" }, "stopped"],
    [{ observedState: "idle" }, "idle"],
  ] as const)("%o reads as %s", (input, expected) => {
    expect(taskStatus(input)).toBe(expected)
  })

  it("treats only waiting, running, queued and paused work as unfinished", () => {
    expect(["waiting", "running", "queued", "paused"].every((status) => isActiveTask(status as never))).toBe(true)
    expect(["done", "failed", "stopped", "idle"].some((status) => isActiveTask(status as never))).toBe(false)
  })

  it("says how long ago, then falls back to a date", () => {
    expect(sinceLabel(new Date(Date.now() - 2 * 3600_000).toISOString(), "en-US")).toBe("2 hours ago")
    expect(sinceLabel("2020-03-04T00:00:00Z", "en-US")).toMatch(/Mar/)
    expect(sinceLabel("not a date", "en-US")).toBe("")
  })
})

describe("plain summary for a task card", () => {
  it("drops markdown marks and keeps the words", () => {
    expect(plainSummary("当前项目文件夹（`/workspace/project-01`）下暂无文件。\n\n*（注：上一级有 `snake.html`）*"))
      .toBe("当前项目文件夹（/workspace/project-01）下暂无文件。\n\n（注：上一级有 snake.html）")
    expect(plainSummary("## 结果\n**已完成**，见 [报告](/app/s/x)\n- 第一项\n* 第二项"))
      .toBe("结果\n已完成，见 报告\n• 第一项\n• 第二项")
    expect(plainSummary("| 元素 | 颜色 |\n| --- | --- |\n| 蛇头 | 翡翠绿 |")).toBe("元素 · 颜色\n\n蛇头 · 翡翠绿")
  })

  it("leaves identifiers with underscores and plain text alone", () => {
    expect(plainSummary("snake_case_name stays")).toBe("snake_case_name stays")
    expect(plainSummary("  plain words  ")).toBe("plain words")
    expect(plainSummary("")).toBe("")
  })
})
