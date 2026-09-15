import { cleanup, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"
import type { TrajectoryEvent } from "../../../types/protocol"
import { inspectorEnv, makeRecord, withInspector } from "../../testing/harness"
import { SummaryPanel } from "./SummaryPanel"
import { ToolResultPanel } from "./ToolResultPanel"

vi.mock("react-i18next", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react-i18next")>()
  const t = (key: string, vars?: Record<string, unknown>) => (vars ? `${key} ${JSON.stringify(vars)}` : key)
  return { ...actual, useTranslation: () => ({ t, i18n: { language: "en-US" } }) }
})

afterEach(cleanup)

const SHA256 = "ab".repeat(32)

function output(seq: string, data: Record<string, unknown>): TrajectoryEvent {
  return {
    event_id: `evt_${seq}`,
    user_id: "usr",
    session_id: "ses_test",
    seq,
    type: "tool.output",
    version: 1,
    occurred_at: "2026-09-15T08:00:00.000Z",
    call_id: "call_log",
    data: { tool: "bash", ...data },
  }
}

function toolRecord(events: TrajectoryEvent[], partial: Parameters<typeof makeRecord>[0] | object = {}) {
  return {
    ...makeRecord({
      record_id: "tool:call_log",
      kind: "tool",
      call_id: "call_log",
      data: { tool: "bash", output: "head\n[... 1048576 bytes omitted ...]\ntail", model_output: "head" },
      ...partial,
    }),
    events,
  }
}

describe("recorded tool output", () => {
  it("says a final output was cut, with its full size and sha256", () => {
    const record = toolRecord([
      output("3", {
        output: "head",
        mode: "delta",
        stage: "executor_stream",
        chunk_index: 0,
        stream_truncated: true,
      }),
      output("4", {
        output: "head\n[... 1048576 bytes omitted ...]\ntail",
        mode: "replace",
        stage: "executor_result",
        final: true,
        output_truncated: true,
        output_bytes: 2 * 1024 * 1024,
        output_sha256: SHA256,
      }),
    ])
    render(withInspector(<ToolResultPanel record={record} />, inspectorEnv([record])))
    const note = screen.getByTestId("trajectory-output-truncated")
    expect(note.textContent).toBe(`tool.outputTruncated ${JSON.stringify({ size: "2 MB", sha256: SHA256 })}`)
  })

  it("says streamed output stopped at the recording limit while the call runs", () => {
    const record = toolRecord(
      [
        output("3", {
          output: "head",
          mode: "delta",
          stage: "executor_stream",
          chunk_index: 0,
          stream_truncated: true,
        }),
      ],
      { status: "running", end_seq: null, finished_at: null },
    )
    render(withInspector(<SummaryPanel record={record} />, inspectorEnv([record])))
    expect(screen.getByTestId("trajectory-output-truncated").textContent).toBe("tool.streamTruncated")
  })

  it("adds no note when the whole output was recorded", () => {
    const record = toolRecord([
      output("3", {
        output: "head",
        mode: "delta",
        stage: "executor_stream",
        chunk_index: 0,
        stream_truncated: true,
      }),
      output("4", { output: "all of it", mode: "replace", stage: "executor_result", final: true }),
    ])
    render(withInspector(<ToolResultPanel record={record} />, inspectorEnv([record])))
    expect(screen.queryByTestId("trajectory-output-truncated")).toBeNull()
  })
})
