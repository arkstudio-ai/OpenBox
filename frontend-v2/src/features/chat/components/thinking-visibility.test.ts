/**
 * When the waiting row should be on screen.
 *
 * It showed whenever prose was absent, so a turn that was reasoning or running
 * a tool — visibly working, each with its own live heading — still carried a
 * second "正在思考中" underneath claiming the model had not responded.
 *
 * The rule is: it stands in for having nothing at all, and steps aside the
 * moment anything arrives.
 */
import { describe, expect, it } from "vitest"

/** Mirrors the guard in AssistantTurn. */
function hasActivity(view: { content: string; thinking: string; tools: unknown[] }): boolean {
  return (
    view.content.trim().length > 0 ||
    view.thinking.trim().length > 0 ||
    view.tools.length > 0
  )
}

const EMPTY = { content: "", thinking: "", tools: [] as unknown[] }

describe("waiting-row visibility", () => {
  it("shows while the turn is genuinely empty", () => {
    expect(hasActivity(EMPTY)).toBe(false)
  })

  it("steps aside once reasoning has arrived", () => {
    // The screenshot case: thinking text present, no prose yet.
    expect(hasActivity({ ...EMPTY, thinking: "Confirming image generation" })).toBe(true)
  })

  it("steps aside once a tool call has started", () => {
    expect(hasActivity({ ...EMPTY, tools: [{ id: "t1" }] })).toBe(true)
  })

  it("steps aside once prose is streaming", () => {
    expect(hasActivity({ ...EMPTY, content: "你好" })).toBe(true)
  })

  it("treats whitespace-only reasoning as nothing", () => {
    // An empty delta must not silently retire the row.
    expect(hasActivity({ ...EMPTY, thinking: "   \n" })).toBe(false)
  })

  it("treats whitespace-only prose as nothing", () => {
    expect(hasActivity({ ...EMPTY, content: "  " })).toBe(false)
  })
})
