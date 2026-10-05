import { afterEach, expect, it, vi } from "vitest"
import type { MessageWithParts } from "@/shared/types/api"
import { createHistoryProofReader, historyProofBarrier, historySourceBatches, rememberHistoryProof, requireFreshHistoryProof } from "./history-source-proof"

const scope = { userId: "batch-owner", workspaceId: "batch-workspace" }
const message = (id: string): MessageWithParts => ({ id, session_id: "batch-session", role: "assistant",
  created_at: "", parts: [], source_status: "available", source_checked_at: "2026-10-06T00:00:00Z" })
afterEach(() => vi.restoreAllMocks())

it("keeps 85 retained messages in one batch after eleven different history responses", () => {
  const sizes = [26, 1, 3, 4, 3, 32, 3, 3, 4, 2, 4]
  const messages: MessageWithParts[] = []
  for (const size of sizes) {
    const page = Array.from({ length: size }, (_, index) => message(`m${messages.length + index}`))
    rememberHistoryProof(page, scope, 100 + messages.length)
    messages.push(...page)
  }
  expect(messages).toHaveLength(85)
  expect(historySourceBatches(messages)).toEqual([messages.map((item) => item.id)])
})

it("keeps bounded ID batches stable across proof updates, additions and removals", () => {
  const messages = Array.from({ length: 205 }, (_, index) => message(`m${index}`))
  const expected = [messages.slice(0, 100), messages.slice(100, 200), messages.slice(200)].map((page) => page.map((item) => item.id))
  expect(historySourceBatches(messages)).toEqual(expected)
  const refreshed = messages.map((item, index) => {
    const next = { ...item }
    rememberHistoryProof([next], scope, index)
    return next
  })
  expect(historySourceBatches(refreshed)).toEqual(expected)
  const appended = historySourceBatches([...refreshed, message("m205"), message("tmp-pending"), refreshed[0]])
  expect(appended).toEqual([expected[0], expected[1], [...expected[2], "m205"]])
  expect(historySourceBatches(refreshed.slice(1)).flat()).toEqual(messages.slice(1).map((item) => item.id))
})

it("consumes only eligible originals once, independently of later batch membership", () => {
  const current = message("current"), old = message("old")
  rememberHistoryProof([current], scope, 200)
  rememberHistoryProof([old], scope, 100)
  const read = createHistoryProofReader()
  expect(read([old, undefined, current], scope, 150)).toEqual([current])
  expect(read([current, old], scope, 150)).toEqual([])
  expect(read([old], scope, 100)).toEqual([old])
  const next = { ...current }
  rememberHistoryProof([next], scope, 300)
  expect(read([next], scope, 150)).toEqual([next])
  // A new proof for the same ID cannot reset consumption of its older object.
  expect(read([current], scope, 150)).toEqual([])
})

it("does not infer proof from fields or consume another scope's proof", () => {
  const current = message("current")
  rememberHistoryProof([current], scope, 200)
  const read = createHistoryProofReader()
  expect(read([{ ...current }], scope, 0)).toEqual([])
  expect(read([current], { ...scope, userId: "peer" }, 0)).toEqual([])
  expect(read([current], { ...scope, workspaceId: "other" }, 0)).toEqual([])
  expect(read([current], scope, 201)).toEqual([])
  expect(read([current], scope, 200)).toEqual([current])
})

it("rejects pre-hint history even on a millisecond tie and advances every hint", () => {
  vi.spyOn(Date, "now").mockReturnValue(100)
  const owner = {}, current = message("current")
  requireFreshHistoryProof(owner, scope, "batch-session")
  const first = historyProofBarrier(owner, scope, "batch-session")
  requireFreshHistoryProof(owner, scope, "batch-session")
  const second = historyProofBarrier(owner, scope, "batch-session")
  expect(first).toBe(101)
  expect(second).toBe(102)
  // The response arrived after both hints, but its read began before them.
  rememberHistoryProof([current], scope, 100)
  expect(createHistoryProofReader()([current], scope, second)).toEqual([])
  expect(historyProofBarrier({}, scope, "batch-session")).toBe(0)
  expect(historyProofBarrier(owner, { ...scope, userId: "peer" }, "batch-session")).toBe(0)
  expect(historyProofBarrier(owner, { ...scope, workspaceId: "other" }, "batch-session")).toBe(0)
  expect(historyProofBarrier(owner, scope, "other-session")).toBe(0)
})
