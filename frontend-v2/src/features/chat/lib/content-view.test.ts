import { describe, expect, it } from "vitest"
import type { MessageWithParts } from "@/shared/types/api"
import { buildAssistantContentView } from "./content-view"

function message(
  id: string,
  parts: MessageWithParts["parts"],
  finish: MessageWithParts["finish"] = null,
): MessageWithParts {
  return {
    id,
    session_id: "session-1",
    role: "assistant",
    parts,
    created_at: "2026-08-31T00:00:00Z",
    finish,
  }
}

describe("buildAssistantContentView", () => {
  it("keeps work narration separate from the final answer and groups its artifact", () => {
    const view = buildAssistantContentView(
      [
        message(
          "step",
          [
            { type: "text", id: "thinking", text: "Rendering", channel: "commentary" },
            {
              type: "tool",
              id: "image-tool",
              tool: "image_gen",
              status: "completed",
              title: "Generated image",
            },
            {
              type: "file",
              id: "image-file",
              path: "/workspace/result.png",
              relation: { source_part_id: "image-tool", group_id: "hero", role: "result" },
            },
          ],
          "tool_calls",
        ),
        message("final", [{ type: "text", id: "answer", text: "Done", channel: "final" }], "stop"),
      ],
      false,
    )

    expect(view.finalText).toBe("Done")
    expect(view.progress.map((item) => item.text)).toEqual(["Rendering"])
    expect(view.resultGroups).toHaveLength(1)
    expect(view.resultGroups[0]).toMatchObject({
      id: "hero",
      artifactKind: "generated_image",
      role: "result",
      label: "Generated image",
    })
  })

  it("projects the latest segment transcript into an earlier video artifact", () => {
    const view = buildAssistantContentView(
      [
        message(
          "video",
          [
            {
              type: "tool",
              id: "project-tool",
              tool: "video_project",
              status: "completed",
              output: [
                "segment_1_id=segment-a",
                "segment_1_revision=2",
                "segment_1_script=Opening scene",
              ].join("\n"),
            },
            {
              type: "file",
              id: "segment-file",
              path: "/workspace/segment.mp4",
              relation: {
                kind: "video_segment",
                role: "intermediate",
                ordinal: 1,
                metadata: { segment_id: "segment-a", production_id: "production-a" },
              },
            },
            {
              type: "tool",
              id: "transcribe-tool",
              tool: "video_transcribe",
              status: "completed",
              input: { segment_id: "segment-a" },
              output: "transcript=Spoken line\nverdict=pass\nsimilarity=0.93",
            },
          ],
          "tool_calls",
        ),
      ],
      false,
    )

    expect(view.resultGroups[0]).toMatchObject({
      id: "video:production-a:segment:segment-a",
      caption: "Opening scene",
      ordinal: 1,
      revision: 2,
      metadata: {
        transcript: "Spoken line",
        stt_verdict: "pass",
        stt_similarity: 0.93,
      },
    })
  })

  it("uses the final computer frame as verification when no richer result exists", () => {
    const view = buildAssistantContentView(
      [
        message(
          "computer",
          [
            { type: "tool", id: "computer-tool", tool: "computer", status: "completed" },
            {
              type: "file",
              id: "screen",
              path: "/workspace/screen.png",
              transient: true,
              relation: { source_part_id: "computer-tool", role: "evidence" },
            },
          ],
          "tool_calls",
        ),
        message("final", [{ type: "text", id: "answer", text: "Verified", channel: "final" }], "stop"),
      ],
      false,
    )

    expect(view.verification?.artifactKind).toBe("computer_screenshot")
    expect(view.workEvents).toHaveLength(0)
  })
})
