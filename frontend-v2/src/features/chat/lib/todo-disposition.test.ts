import { describe, expect, it } from "vitest"
import { isInterruptionMarker, mergeTurns, todoDisposition, type TodoView } from "./turn-view"
import type { TodoItem } from "@/shared/types/api"

function item(status: TodoItem["status"], subject = "步骤"): TodoItem {
  return { id: `t-${status}-${subject}`, subject, status, priority: "medium", source: "model" }
}

function view(statuses: TodoItem["status"][], overrides: Partial<TodoView> = {}): TodoView {
  const items = statuses.map((s, i) => item(s, `步骤${i + 1}`))
  const counted = items.filter((i) => i.status !== "cancelled")
  const done = counted.filter((i) => i.status === "completed").length
  const activeIndex = counted.findIndex((i) => i.status === "in_progress")
  return {
    tasks: items.map((it) => ({ item: it, tools: [] })),
    before: [],
    after: [],
    activeForm: null,
    done,
    total: counted.length,
    current: activeIndex >= 0 ? activeIndex + 1 : done,
    allDone: counted.length > 0 && done === counted.length,
    ...overrides,
  }
}

describe("todoDisposition", () => {
  it("only a running turn may look alive", () => {
    const running = view(["completed", "in_progress", "pending"])
    expect(todoDisposition(running, true)).toEqual({ kind: "live" })
  })

  it("the same card is interrupted once its turn ends", () => {
    // The bug this whole change exists for: a stopped task went on creeping
    // toward 90% for as long as the page stayed open.
    const running = view(["completed", "in_progress", "pending"])
    expect(todoDisposition(running, false)).toEqual({ kind: "interrupted", at: 2 })
  })

  it("an unrelated later turn cannot relight an old card", () => {
    // The trap in the obvious design: keying "live" off session-busy meant a
    // brand-new, unrelated turn made every earlier card animate again. The
    // predicate is per-turn, so a card whose own turn is over stays settled
    // no matter what else the session is doing.
    const old = view(["completed", "in_progress", "pending"])
    expect(todoDisposition(old, false).kind).toBe("interrupted")
  })

  it("finishing wins over liveness", () => {
    const all = view(["completed", "completed"])
    expect(todoDisposition(all, true)).toEqual({ kind: "done" })
    expect(todoDisposition(all, false)).toEqual({ kind: "done" })
  })

  it("ending with work left but nothing mid-flight is merely unfinished", () => {
    // Not every stop interrupts something: the model can close its last task
    // and simply stop. Calling that "interrupted" would overstate it.
    expect(todoDisposition(view(["completed", "pending"]), false)).toEqual({
      kind: "unfinished",
    })
  })

  it("a cancelled task does not count as running", () => {
    expect(todoDisposition(view(["completed", "cancelled"]), false).kind).toBe("done")
  })

  it("reports the stopping point by its counted position", () => {
    // Cancelled items are excluded from the count, so the reported step has to
    // match what the counter above it shows.
    const v = view(["cancelled", "completed", "in_progress"])
    const d = todoDisposition(v, false)
    expect(d).toEqual({ kind: "interrupted", at: 2 })
  })

  it("liveness is a fact about the turn, not about the list", () => {
    // An empty list on a running turn is still a running turn; it just has
    // nothing to show yet. Settled, it is unfinished rather than interrupted.
    expect(todoDisposition(view([]), true).kind).toBe("live")
    expect(todoDisposition(view([]), false).kind).toBe("unfinished")
  })
})

describe("mergeTurns · the interruption marker", () => {
  function msg(over: Record<string, unknown>) {
    return {
      id: "m1", session_id: "s", role: "user", parts: [], created_at: "",
      ...over,
    } as never
  }
  const syntheticText = [{ type: "text", text: "[已中断]", synthetic: true }]

  it("keeps the marker even though it is synthetic", () => {
    // It is synthetic because the model must read it, but it is also the only
    // record that a turn was cut short — dropping it left the transcript
    // jumping from half-finished work to whatever came next.
    const turns = mergeTurns([
      msg({ id: "u1", parts: [{ type: "text", text: "做事" }] }),
      msg({ id: "mk", client_message_id: "tabort:s:1", parts: syntheticText }),
    ])
    expect(turns.map((t) => t.key)).toEqual(["u1", "mk"])
  })

  it("still drops the internal prompts it was written to hide", () => {
    const turns = mergeTurns([
      msg({ id: "u1", parts: [{ type: "text", text: "做事" }] }),
      msg({ id: "sji", client_message_id: "sji:x", parts: syntheticText }),
    ])
    expect(turns.map((t) => t.key)).toEqual(["u1"])
  })

  it("recognises a marker only by its reserved prefix", () => {
    expect(isInterruptionMarker({ client_message_id: "tabort:s:1" })).toBe(true)
    expect(isInterruptionMarker({ client_message_id: "cmid-user-typed" })).toBe(false)
    expect(isInterruptionMarker({})).toBe(false)
  })
})
