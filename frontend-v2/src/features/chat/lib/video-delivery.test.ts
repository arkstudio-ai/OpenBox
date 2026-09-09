import { describe, expect, it } from "vitest"
import type { FilePart, MessageWithParts, ToolPart } from "@/shared/types/api"
import fixture from "./__fixtures__/direct-video-messages.json"
import { buildAssistantContentView } from "./content-view"

const transcript = () => structuredClone(fixture) as MessageWithParts[]
const generated = (messages: MessageWithParts[]) => messages[0].parts[1] as FilePart
const shared = (messages: MessageWithParts[]) => messages[1].parts[1] as FilePart
const generator = (messages: MessageWithParts[]) => messages[0].parts[0] as ToolPart
const sharing = (messages: MessageWithParts[]) => messages[1].parts[0] as ToolPart
const finalKinds = (messages: MessageWithParts[], streaming = false, awaitingInput = false) =>
  buildAssistantContentView(messages, streaming, awaitingInput).resultGroups.map((g) => g.artifactKind)

describe("standalone video delivered through share_file", () => {
  it("recognizes the production-shaped transcript without changing persisted parts", () => {
    const messages = transcript()
    const before = JSON.stringify(messages)
    const groups = buildAssistantContentView(messages, false).resultGroups
    expect(groups.map((g) => [g.artifactKind, g.role])).toEqual([
      ["video_segment", "intermediate"],
      ["video_final", "final"],
    ])
    expect(groups[1].parts[0].asset_id).toBe("asset-shared")
    expect(JSON.stringify(messages)).toBe(before)
  })

  it("uses asset identity when sharing reuses the original asset", () => {
    const messages = transcript()
    shared(messages).asset_id = generated(messages).asset_id
    generator(messages).output = "status=completed"
    expect(finalKinds(messages)).toContain("video_final")
  })

  it("supports old finished transcripts without message finish/channel metadata", () => {
    const messages = transcript()
    for (const message of messages) delete message.finish
    messages[2].parts = [{ type: "text", id: "old-final", text: "Delivered" }]
    expect(finalKinds(messages)).toContain("video_final")
  })

  it.each([
    [
      "unrelated video with the same basename",
      (m: MessageWithParts[]) => {
        shared(m).path = "/another/segment-video_fixture.mp4"
      },
    ],
    [
      "missing generated provenance",
      (m: MessageWithParts[]) => {
        generator(m).output = "status=completed"
      },
    ],
    [
      "failed generation",
      (m: MessageWithParts[]) => {
        generator(m).output = "status=failed"
      },
    ],
    [
      "running generation",
      (m: MessageWithParts[]) => {
        generator(m).status = "running"
      },
    ],
    [
      "failed share",
      (m: MessageWithParts[]) => {
        sharing(m).status = "error"
      },
    ],
    [
      "non-attached share",
      (m: MessageWithParts[]) => {
        sharing(m).metadata = { attached: false }
      },
    ],
    [
      "explicitly disabled attachment",
      (m: MessageWithParts[]) => {
        sharing(m).input = { attach: false }
      },
    ],
    [
      "explicit intermediate preview",
      (m: MessageWithParts[]) => {
        shared(m).relation!.role = "intermediate"
      },
    ],
    [
      "production segment preview",
      (m: MessageWithParts[]) => {
        generated(m).relation!.metadata = { production_id: "production-1", segment_id: "segment-1" }
      },
    ],
    [
      "non-video attachment",
      (m: MessageWithParts[]) => {
        shared(m).mime_type = "image/png"
      },
    ],
    [
      "attachment without an asset",
      (m: MessageWithParts[]) => {
        delete shared(m).asset_id
      },
    ],
    [
      "only final prose, no shared file",
      (m: MessageWithParts[]) => {
        m[1].parts.pop()
      },
    ],
    [
      "no final reply",
      (m: MessageWithParts[]) => {
        m.pop()
      },
    ],
    [
      "aborted answer",
      (m: MessageWithParts[]) => {
        m[2].finish = "aborted"
      },
    ],
    [
      "waiting answer",
      (m: MessageWithParts[]) => {
        m[2].finish = "waiting_input"
      },
    ],
    [
      "error on final message",
      (m: MessageWithParts[]) => {
        m[2].error = { message: "Disconnected" }
      },
    ],
    [
      "share predates generation",
      (m: MessageWithParts[]) => {
        ;[m[0], m[1]] = [m[1], m[0]]
      },
    ],
    [
      "ambiguous second material",
      (m: MessageWithParts[]) => {
        m[0].parts.push({
          ...generated(m),
          id: "other",
          relation: { kind: "video_segment", group_id: "other-material" },
        })
      },
    ],
  ])("does not promote %s", (_, mutate) => {
    const messages = transcript()
    mutate(messages)
    expect(finalKinds(messages)).not.toContain("video_final")
  })

  it("waits for delivery completion, not just a file during an active or suspended turn", () => {
    expect(finalKinds(transcript(), true)).not.toContain("video_final")
    expect(finalKinds(transcript(), false, true)).not.toContain("video_final")
  })
})
