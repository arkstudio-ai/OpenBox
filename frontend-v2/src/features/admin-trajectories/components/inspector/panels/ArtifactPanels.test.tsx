import { cleanup, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"
import type { TrajectoryEvent } from "../../../types/protocol"
import { inspectorEnv, makeRecord, withInspector } from "../../testing/harness"
import { ArtifactDiffPanel } from "./ArtifactDiffPanel"
import { ArtifactPreviewPanel } from "./ArtifactPreviewPanel"
import { ArtifactVersionsPanel } from "./ArtifactVersionsPanel"

vi.mock("react-i18next", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react-i18next")>()
  const t = (key: string, vars?: Record<string, unknown>) => (vars ? `${key} ${JSON.stringify(vars)}` : key)
  return { ...actual, useTranslation: () => ({ t, i18n: { language: "en-US" } }) }
})

afterEach(cleanup)

const DIFF = "--- /workspace/report.md\n+++ /workspace/report.md\n@@ -1 +1 @@\n-状态：草稿\n+状态：已完成\n"

function fileRecord(data: Record<string, unknown>) {
  return makeRecord({
    record_id: "artifact:file:/workspace/report.md",
    kind: "artifact",
    call_id: "call_file",
    start_seq: "5",
    end_seq: "5",
    as_of_seq: "9",
    data: {
      artifact_type: "file_diff",
      name: "report.md",
      path: "/workspace/report.md",
      availability: "available",
      capture_level: "executor_content",
      ...data,
    },
  })
}

describe("file change artifacts", () => {
  it("shows both retained versions and the captured unified diff", () => {
    const record = fileRecord({
      operation: "edit",
      before: {
        availability: "available",
        text: "状态：草稿\n",
        sha256: "aa11",
        size_bytes: 16,
        source: "executor_content",
      },
      after: {
        availability: "available",
        text: "状态：已完成\n",
        sha256: "bb22",
        size_bytes: 19,
        source: "executor_content",
        redacted: true,
      },
      diff: DIFF,
    })
    const env = inspectorEnv([record])
    const { container, unmount } = render(withInspector(<ArtifactVersionsPanel record={record} />, env))
    expect(screen.getByTestId("trajectory-version-before").getAttribute("data-availability")).toBe(
      "available",
    )
    expect(screen.getByText("aa11")).toBeTruthy()
    expect(screen.getByText("artifact.redactedNote")).toBeTruthy()
    expect(container.textContent).toContain("状态：已完成")
    unmount()
    const preview = render(withInspector(<ArtifactPreviewPanel record={record} />, env))
    const lines = [...preview.container.querySelectorAll("[data-line]")].map((line) =>
      line.getAttribute("data-line"),
    )
    expect(lines).toEqual(["header", "header", "hunk", "del", "add"])
  })

  it("says a missing before version was not recorded, and that no diff could be made", () => {
    const record = fileRecord({
      operation: "write",
      before: { availability: "not_recorded" },
      after: {
        availability: "available",
        text: "new file\n",
        sha256: "cc33",
        size_bytes: 9,
        source: "executor_content",
      },
      diff: null,
    })
    const env = inspectorEnv([record])
    render(withInspector(<ArtifactVersionsPanel record={record} />, env))
    expect(screen.getByTestId("trajectory-version-before").getAttribute("data-availability")).toBe(
      "not_recorded",
    )
    cleanup()
    const { container } = render(withInspector(<ArtifactDiffPanel record={record} />, env))
    expect(container.querySelector("[data-availability=not_recorded]")).not.toBeNull()
    expect(screen.getByText("artifact.noDiff")).toBeTruthy()
  })

  it("distinguishes a file confirmed absent after a delete", () => {
    const record = fileRecord({
      operation: "delete",
      before: {
        availability: "available",
        text: "old\n",
        sha256: "dd44",
        size_bytes: 4,
        source: "executor_content",
      },
      after: { availability: "absent" },
      diff: null,
    })
    render(withInspector(<ArtifactVersionsPanel record={record} />, inspectorEnv([record])))
    expect(screen.getByTestId("trajectory-version-after").getAttribute("data-availability")).toBe("absent")
    expect(screen.getByText("artifact.absent")).toBeTruthy()
  })

  it("lists every recorded write up to the position", () => {
    const first = {
      artifact_type: "file_diff",
      path: "/workspace/report.md",
      operation: "write",
      before: { availability: "not_recorded" },
      after: { availability: "available", text: "a\n" },
      diff: null,
    }
    const second = {
      ...first,
      operation: "edit",
      before: { availability: "available", text: "a\n" },
      after: { availability: "available", text: "b\n" },
      diff: "@@ -1 +1 @@\n-a\n+b\n",
    }
    const event = (seq: string, data: Record<string, unknown>): TrajectoryEvent => ({
      event_id: `evt_${seq}`,
      user_id: "usr",
      session_id: "ses_test",
      seq,
      type: "artifact.recorded",
      version: 1,
      occurred_at: "2026-09-11T08:00:00.000Z",
      call_id: "call_file",
      data: { artifact_id: "file:/workspace/report.md", ...data },
    })
    const record = {
      ...fileRecord(second),
      record_id: "artifact:file:/workspace/report.md",
      events: [event("5", first), event("9", second)],
    }
    render(withInspector(<ArtifactDiffPanel record={record} />, inspectorEnv([record])))
    expect(screen.getAllByTestId("trajectory-file-revision")).toHaveLength(2)
  })
})
