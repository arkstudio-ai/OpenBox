import { describe, expect, it } from "vitest"
import type { ToolPart } from "@/shared/types/api"
import { memoryReceipt } from "./memory-receipt"

const part = (tool: string, output: unknown, extra: Partial<ToolPart> = {}): ToolPart => ({
  type: "tool", id: "part", tool, status: "completed",
  output: typeof output === "string" ? output : JSON.stringify(output), ...extra,
})

describe("memory tool receipts", () => {
  it("reads a fresh memory with its id, summary and revision from the live output", () => {
    expect(memoryReceipt(part("memory.remember", { state: "remembered", memory_id: "m1", summary: "Prefers tables",
      revision: 1, scope: "personal", project_id: null, undo: "…" })))
      .toEqual({ kind: "remembered", memoryId: "m1", summary: "Prefers tables", revision: 1 })
  })

  it("keeps working from the persisted metadata once the transcript is reloaded", () => {
    const reloaded = part("memory.remember", "", { output: undefined,
      metadata: { assistant_memory: { state: "remembered", memory_id: "m1", summary: "Prefers tables", scope: "personal" } } })
    expect(memoryReceipt(reloaded)).toEqual({ kind: "remembered", memoryId: "m1", summary: "Prefers tables", revision: undefined })
  })

  it("prefers the persisted metadata over the output where both are present", () => {
    const both = part("memory.update", { state: "updated", memory_id: "m1", summary: "old", revision: 3 },
      { metadata: { assistant_memory: { state: "updated", memory_id: "m1", summary: "Answers in tables" } } })
    expect(memoryReceipt(both)).toEqual({ kind: "updated", memoryId: "m1", summary: "Answers in tables" })
  })

  it.each([
    ["memory.remember", { state: "already_remembered", memory_id: "m1" }, { kind: "already_remembered" }],
    ["memory.remember", { state: "refused", reason: "credential" }, { kind: "refused" }],
    ["memory.update", { state: "refused", reason: "credential" }, { kind: "refused" }],
    ["memory.remember", { state: "paused" }, { kind: "paused" }],
    ["memory.forget", { state: "forgotten", memory_id: "m1", note: "…" }, { kind: "forgotten" }],
  ])("reads %s %j", (tool, output, expected) => {
    expect(memoryReceipt(part(tool, output))).toEqual(expected)
  })

  it.each([
    ["a call still running", part("memory.remember", { state: "remembered", memory_id: "m1", summary: "x" }, { status: "running" })],
    ["another tool", part("memory.search", { state: "remembered", memory_id: "m1", summary: "x" })],
    ["prose output", part("memory.remember", "I remembered that you prefer tables.")],
    ["an answered confirmation card", part("memory.remember", "记住")],
    ["a remembered state without an id", part("memory.remember", { state: "remembered", summary: "x" })],
    ["a state of another tool", part("memory.forget", { state: "remembered", memory_id: "m1", summary: "x" })],
  ])("ignores %s", (_label, input) => {
    expect(memoryReceipt(input)).toBeNull()
  })
})
