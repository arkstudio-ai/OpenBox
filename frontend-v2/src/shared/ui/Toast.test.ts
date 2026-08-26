/**
 * The toast is the only channel some failures have, so its store has to hold
 * what those failures need: enough time to be read, no duplicate pile-up, and
 * a way to stay put until dismissed.
 */
import { beforeEach, describe, expect, it } from "vitest"
import { toast, useToastStore } from "./Toast"

describe("toast store", () => {
  beforeEach(() => useToastStore.getState().clear())

  it("keeps a long message on screen longer than a short one", () => {
    // A fixed 3.2s suited "Saved" and lost every sentence longer than it.
    toast.info("好")
    toast.error("对话数量已达上限（200/200），无法新建对话。请删除一些不再需要的旧对话后重试——已有对话可以继续聊。")
    const [short, long] = useToastStore.getState().items
    expect(long.duration).toBeGreaterThan(short.duration)
  })

  it("never sits for less than a few seconds, however short the text", () => {
    toast.success("好")
    expect(useToastStore.getState().items[0].duration).toBeGreaterThanOrEqual(3_600)
  })

  it("caps how long it lingers, however long the text", () => {
    toast.error("很长的错误。".repeat(200))
    expect(useToastStore.getState().items[0].duration).toBeLessThanOrEqual(12_000)
  })

  it("collapses a repeat of the same message instead of stacking it", () => {
    // Retries produce the same refusal; three identical cards say nothing more
    // than one and bury whatever is underneath.
    const first = toast.error("配额已满")
    const second = toast.error("配额已满")
    expect(second).toBe(first)
    expect(useToastStore.getState().items).toHaveLength(1)
  })

  it("treats the same words of a different kind as its own message", () => {
    toast.info("完成")
    toast.error("完成")
    expect(useToastStore.getState().items).toHaveLength(2)
  })

  it("honours a duration of 0 as stay-until-dismissed", () => {
    toast.error("需要用户确认", { duration: 0 })
    expect(useToastStore.getState().items[0].duration).toBe(0)
  })

  it("carries an optional title", () => {
    toast.warning("详情在这里", { title: "注意" })
    expect(useToastStore.getState().items[0].title).toBe("注意")
  })

  it("dismisses by the id it handed back", () => {
    const id = toast.info("待关闭")
    toast.dismiss(id)
    expect(useToastStore.getState().items).toHaveLength(0)
  })
})
