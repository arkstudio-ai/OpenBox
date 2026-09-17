import { describe, expect, it } from "vitest"
import type { MessageWithParts } from "@/shared/types/api"
import { buildCompactionViews } from "./compaction-view"
import { buildAssistantContentView } from "./content-view"
import { mergeTurns } from "./turn-view"

function reply(id: string, extra: Partial<MessageWithParts> = {}): MessageWithParts {
  return { id, role: "assistant", session_id: "s", created_at: "", parts: [], ...extra }
}

const request = (auto: boolean): MessageWithParts => reply("compact", {
  role: "user", agent: "compaction", parts: [
    { type: "text", id: "empty", text: "", synthetic: true },
    { type: "compaction", id: "marker", auto },
  ],
})
const summary = (extra: Partial<MessageWithParts> = {}): MessageWithParts => reply("summary", {
  agent: "compaction", parent_id: "compact", summary: false,
  parts: [{ type: "text", id: "summary-text", text: "## Goal\nKeep the important constraints." }],
  ...extra,
})
const answer = reply("answer", {
  finish: "stop", tokens: { input: 20, output: 5, total: 25, cache: 0, limit: 1000, cost: 0, context: 25 },
  parts: [{ type: "text", id: "answer-text", text: "The task is complete.", channel: "final" }],
})

describe("compaction is a process item throughout its lifecycle", () => {
  it("shows a running item as soon as the internal request arrives", () => {
    expect(buildCompactionViews([request(true)], true)).toEqual([
      { id: "compact", status: "running", summary: "" },
    ])
  })

  it("folds streaming text before summary=true is persisted, without creating answer prose", () => {
    const messages = [request(true), summary()]
    expect(buildCompactionViews(messages, true)[0]).toMatchObject({ id: "compact", status: "running" })
    const content = buildAssistantContentView(messages, true)
    expect(content.hasFinal).toBe(false)
    expect(content.progress).toEqual([])
    expect(content.workEvents).toEqual([])
  })

  it("keeps one stable item when completion and the next loop step arrive", () => {
    const messages = [request(true), summary({ summary: true, finish: "stop" }), answer]
    expect(buildCompactionViews(messages, true)).toHaveLength(1)
    expect(buildCompactionViews(messages, true)[0].status).toBe("completed")
    const content = buildAssistantContentView(messages, false)
    expect(content.finalMessageId).toBe("answer")
    expect(content.finalText).toBe("The task is complete.")
    expect(content.progress).toEqual([])
  })

  it("recovers a summary whose request is outside the loaded page", () => {
    const completed = summary({ summary: true, finish: "stop" })
    expect(buildCompactionViews([completed], false)).toEqual(
      buildCompactionViews([request(true), completed], false),
    )
  })

  it("supports old stored summaries and completed descriptors without text", () => {
    expect(buildCompactionViews([summary({ agent: undefined, summary: true, finish: "stop" })], false)[0].status).toBe("completed")
    const committed = request(true)
    committed.parts = [{ type: "compaction", id: "marker", replacement_id: "replacement" }]
    expect(buildCompactionViews([committed], false)[0]).toMatchObject({ status: "completed", summary: "" })
  })

  it("does not call a failed or interrupted partial summary a successful answer", () => {
    expect(buildCompactionViews([request(true), summary({ error: { message: "provider failed" } })], false)[0].status).toBe("failed")
    expect(buildCompactionViews([request(true), summary()], false)[0].status).toBe("interrupted")
    expect(buildAssistantContentView([summary({ error: { message: "provider failed" } })], false).hasFinal).toBe(false)
  })

  it("does not revive an unfinished old attempt when a new loop step starts", () => {
    expect(buildCompactionViews([request(true), summary(), reply("next")], true)[0].status).toBe("interrupted")
  })

  it("keeps separate retries visible without duplicating a request and its summary", () => {
    const second = { ...request(true), id: "compact-2" }
    const views = buildCompactionViews([
      request(true), summary({ summary: true, finish: "stop" }), second,
      summary({ id: "summary-2", parent_id: "compact-2" }),
    ], true)
    expect(views.map((view) => view.status)).toEqual(["completed", "running"])
  })
})

describe("turn grouping around context optimization", () => {
  it("keeps automatic compaction and continuation inside the same visible turn", () => {
    const before = reply("before", { finish: "tool_calls" })
    const continueMessage = reply("continue", {
      role: "user", parts: [{ type: "text", id: "continue-text", text: "Continue", synthetic: true }],
    })
    const turns = mergeTurns([before, request(true), summary({ finish: "stop" }), continueMessage, answer])
    expect(turns).toHaveLength(1)
    expect(turns[0].kind).toBe("assistant")
    if (turns[0].kind !== "assistant") throw new Error("Expected assistant turn")
    expect(turns[0].meta.messageId).toBe("answer")
    expect(turns[0].messages.map((message) => message.id)).toEqual(["before", "compact", "summary", "answer"])
  })

  it("does not replace reply metadata with summary tokens or failure", () => {
    const turns = mergeTurns([answer, request(true), summary({ error: { message: "failed" } })])
    if (turns[0].kind !== "assistant") throw new Error("Expected assistant turn")
    expect(turns[0].meta.messageId).toBe("answer")
    expect(turns[0].meta.tokens).toEqual(answer.tokens)
    expect(turns[0].meta.error).toBeUndefined()
  })

  it("keeps manual optimization with the previous turn's process and answer", () => {
    const turns = mergeTurns([answer, request(false), summary({ summary: true, finish: "stop" })])
    expect(turns.map((turn) => turn.kind)).toEqual(["assistant"])
    if (turns[0].kind !== "assistant") throw new Error("Expected assistant turn")
    expect(buildAssistantContentView(turns[0].messages, false).finalMessageId).toBe("answer")
    expect(turns[0].meta.messageId).toBe("answer")
  })
})
