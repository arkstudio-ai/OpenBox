import { describe, expect, it } from "vitest"
import type { TrajectoryEvent } from "../types/protocol"
import { fileDiffOf, fileRevisions } from "./artifact"
import { classifyValue, fieldState } from "./availability"
import { replay } from "./projector"
import { tabsFor } from "./tabs"

function artifactEvent(seq: number, data: Record<string, unknown>): TrajectoryEvent {
  return {
    event_id: `evt_${seq}`,
    user_id: "owner",
    session_id: "ses",
    seq: String(seq),
    type: "artifact.recorded",
    version: 1,
    occurred_at: `2026-09-11T08:00:0${seq}.000Z`,
    run_id: "run_a",
    call_id: "call_write",
    data: {
      artifact_id: "file:ses:/workspace/report.md",
      artifact_type: "file_diff",
      name: "report.md",
      path: "/workspace/report.md",
      media_type: "text/plain",
      availability: "available",
      capture_level: "executor_content",
      ...data,
    },
  }
}

const version = (text: string) => ({
  availability: "available",
  text,
  sha256: `sha-${text}`,
  size_bytes: text.length,
  source: "executor_content",
  redacted: false,
})

const events = [
  artifactEvent(1, {
    operation: "create",
    before: { availability: "absent" },
    after: version("draft"),
    diff: null,
  }),
  artifactEvent(2, {
    operation: "update",
    before: version("draft"),
    after: version("completed"),
    diff: "--- a\n+++ b\n-draft\n+completed\n",
  }),
]

describe("file diff artifacts", () => {
  it("shows the latest version and keeps earlier revisions from the log", () => {
    const record = replay(events).records["artifact:file:ses:/workspace/report.md"]
    const artifact = fileDiffOf(record)!
    expect(artifact).toMatchObject({
      path: "/workspace/report.md",
      operation: "update",
      captureLevel: "executor_content",
    })
    expect(artifact.before).toMatchObject({
      text: "draft",
      sha256: "sha-draft",
      sizeBytes: 5,
      field: { state: "available" },
    })
    expect(artifact.diff).toEqual({ state: "available", value: "--- a\n+++ b\n-draft\n+completed\n" })
    const revisions = fileRevisions(record, events)
    expect(revisions.map((revision) => [revision.seq, revision.operation])).toEqual([
      ["1", "create"],
      ["2", "update"],
    ])
    expect(revisions[0].before.field).toEqual({ state: "absent" })
    expect(revisions[0].diff).toEqual({ state: "not_recorded" })
  })

  it("never shows a later revision at an earlier position", () => {
    const early = replay(events.slice(0, 1)).records["artifact:file:ses:/workspace/report.md"]
    expect(fileDiffOf(early)?.after.text).toBe("draft")
    expect(fileRevisions(early, events).map((revision) => revision.seq)).toEqual(["1"])
  })

  it("marks a missing before version as not recorded, never as an empty file", () => {
    const record = replay([artifactEvent(1, { operation: "update", after: version("x") })]).records[
      "artifact:file:ses:/workspace/report.md"
    ]
    expect(fileDiffOf(record)?.before.field).toEqual({ state: "not_recorded" })
    const notCaptured = replay([
      artifactEvent(1, {
        operation: "update",
        before: { availability: "not_recorded" },
        after: version("x"),
      }),
    ]).records["artifact:file:ses:/workspace/report.md"]
    expect(fileDiffOf(notCaptured)?.before.field).toEqual({ state: "not_recorded" })
    const removed = replay([
      artifactEvent(1, {
        operation: "update",
        before: { availability: "deleted", reason: "explicitly_deleted" },
        after: version("x"),
      }),
    ]).records["artifact:file:ses:/workspace/report.md"]
    expect(fileDiffOf(removed)?.before.field).toEqual({ state: "deleted", reason: "explicitly_deleted" })
  })

  it("adds a diff tab to file changes only", () => {
    const record = replay(events).records["artifact:file:ses:/workspace/report.md"]
    expect(tabsFor(record)).toEqual(["summary", "preview", "diff", "versions", "source", "events"])
    expect(tabsFor({ ...record, data: { ...record.data, artifact_type: "image" } })).toEqual([
      "summary",
      "preview",
      "versions",
      "source",
      "events",
    ])
  })
})

describe("open versus closed emptiness", () => {
  const base = replay([{ ...events[0], type: "tool.requested", call_id: "c", data: { name: "bash" } }])
    .records["tool:c"]

  it("does not call an open record's empty output empty", () => {
    const open = { ...base, data: { output: "" } }
    expect(fieldState(open, "output", true)).toEqual({ state: "pending" })
    expect(fieldState({ ...open, status: "completed", end_seq: "9" }, "output", true)).toEqual({
      state: "empty",
    })
  })

  it("keeps genuinely empty inputs empty while running", () => {
    expect(fieldState({ ...base, data: { requested_arguments: {} } }, "requested_arguments")).toEqual({
      state: "empty",
    })
  })

  it("turns retained input media into a protected payload reference, never a URL", () => {
    const media = {
      $media: {
        payload_id: "pay_img",
        sha256: "abc",
        media_type: "image/png",
        size_bytes: 68,
        availability: "available",
      },
      source_asset_id: null,
      source_kind: "inline_non_asset",
      original_encoding: "base64",
      declared_media_type: "image/png",
    }
    expect(classifyValue(base, true, media)).toEqual({
      state: "payload",
      ref: {
        payload_id: "pay_img",
        sha256: "abc",
        media_type: "image/png",
        size_bytes: 68,
        availability: "available",
      },
    })
    expect(
      classifyValue(base, true, {
        $media: { availability: "not_recorded", reason: "ambiguous_asset_source" },
      }),
    ).toEqual({ state: "not_recorded" })
    expect(
      classifyValue(base, true, { $media: { availability: "deleted", reason: "source_attachment_deleted" } }),
    ).toEqual({
      state: "deleted",
      reason: "source_attachment_deleted",
    })
    expect(classifyValue(base, true, { $media: { availability: "available" } })).toEqual({
      state: "not_recorded",
    })
  })

  it("honours availability wrappers", () => {
    expect(classifyValue(base, true, { availability: "not_applicable" })).toEqual({ state: "not_applicable" })
    expect(classifyValue(base, true, { availability: "corrupt" })).toEqual({ state: "corrupt" })
  })
})
