import { describe, expect, it } from "vitest"
import type { MessageWithParts } from "@/shared/types/api"
import { buildAssistantContentView } from "./content-view"

function message(finish: string, status: "waiting_input" | "completed" = "completed"): MessageWithParts {
  return {
    id: "m",
    session_id: "s",
    role: "assistant",
    created_at: "",
    finish,
    parts: [{ type: "tool", id: "p", tool: "question", status }],
  }
}

describe("durable ask is not a failed final answer", () => {
  it("does not show an ended-without-answer warning while waiting", () => {
    expect(buildAssistantContentView([message("waiting_input", "waiting_input")], false).incomplete).toBe(
      false,
    )
  })
  it("does not show that warning while an accepted answer is queued", () => {
    expect(buildAssistantContentView([message("tool_calls")], false, true).incomplete).toBe(false)
  })
  it("keeps cancelled or superseded suspended history free of the generic warning", () => {
    expect(buildAssistantContentView([message("waiting_input")], false).incomplete).toBe(false)
  })
  it("handles a waiting tool before message-finish metadata arrives", () => {
    expect(buildAssistantContentView([message("tool_calls", "waiting_input")], false).incomplete).toBe(false)
  })
  it("still warns about a genuinely finished turn without final prose", () => {
    expect(buildAssistantContentView([message("stop")], false).incomplete).toBe(true)
  })
  it("does not warn when a tool deliberately yields to independent work", () => {
    const result = message("stop")
    result.parts = [
      { type: "tool", id: "p", tool: "team_wait", status: "completed", metadata: { turn_yield: true } },
    ]
    expect(buildAssistantContentView([result], false).incomplete).toBe(false)
    result.parts = [
      { type: "tool", id: "p", tool: "team_wait", status: "completed", metadata: { turn_yield: false } },
    ]
    expect(buildAssistantContentView([result], false).incomplete).toBe(true)
  })
  it("shows the final answer once the resumed turn finishes", () => {
    const result = message("stop")
    result.parts.push({ type: "text", id: "answer", text: "Confirmed", channel: "final" })
    const view = buildAssistantContentView([result], false)
    expect(view.finalText).toBe("Confirmed")
    expect(view.incomplete).toBe(false)
  })
})

describe("video artifact reading order", () => {
  it("orders materials by segment number before the final, preserving ties and unknown ordinals", () => {
    const result = message("stop")
    result.parts = [
      { type: "file", id: "final", path: "final.mp4", relation: { kind: "video_final", role: "final" } },
      { type: "file", id: "second", path: "second.mp4", relation: { kind: "video_segment", ordinal: 2 } },
      { type: "file", id: "first", path: "first.mp4", relation: { kind: "video_segment", ordinal: 1 } },
      { type: "file", id: "revision", path: "revision.mp4", relation: { kind: "video_segment", ordinal: 1 } },
      { type: "file", id: "unknown", path: "unknown.mp4", relation: { kind: "video_segment" } },
    ]
    expect(buildAssistantContentView([result], false).resultGroups.map((group) => group.parts[0].id)).toEqual(
      ["first", "revision", "second", "unknown", "final"],
    )
  })
})

describe("team completion is an ordinary final answer", () => {
  const completed = () => {
    const result = message("stop")
    result.parts = [{ type: "tool", id: "finish", tool: "team_finish", status: "completed",
      metadata: { turn_yield: true },
      output: JSON.stringify({ state: "completing", final_status: "completed", turn_yield: true, summary: "7+8=15。已验算。" }),
    }]
    return result
  }
  it("recovers a historical result and the receipt-to-final commit gap", () => {
    const view = buildAssistantContentView([completed()], false)
    expect(view.finalText).toBe("7+8=15。已验算。")
    expect(view.finalMessageId).toBe("m")
    expect(view.hasFinal).toBe(true)
    expect(view.incomplete).toBe(false)
  })
  it("renders canonical final prose once and keeps preceding text as progress", () => {
    const result = completed()
    result.parts.unshift({ type: "text", id: "intro", text: "正在汇总", channel: "commentary" })
    result.parts.push({ type: "text", id: "answer", text: "7+8=15。已验算。", channel: "final" })
    const view = buildAssistantContentView([result], false)
    expect(view.finalText).toBe("7+8=15。已验算。")
    expect(view.progress.map((p) => p.text)).toEqual(["正在汇总"])
  })
  it("replaces historical pre-finish narration mislabeled as final", () => {
    const result = completed()
    result.parts.unshift({ type: "text", id: "old-preface", text: "可以收尾了。", channel: "final" })
    expect(buildAssistantContentView([result], false).finalText).toBe("7+8=15。已验算。")
  })
  it("does not promote a refused finish or tool arguments into a final answer", () => {
    const result = completed()
    result.parts = [{ type: "tool", id: "finish", tool: "team_finish", status: "error",
      input: { summary: "7+8=15" }, output: JSON.stringify({ code: "DELIVERABLES_INCOMPLETE" }) }]
    expect(buildAssistantContentView([result], false).hasFinal).toBe(false)
  })
})
