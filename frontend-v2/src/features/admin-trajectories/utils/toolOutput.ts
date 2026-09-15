// Tool output as the recorder bounds it (SPEC §5.5): streamed `tool.output`
// records stop at the recording cap, and a final output above the cap keeps
// only its beginning and end, with the full size and digest.
import type { TrajectoryEvent } from "../types/protocol"

export type OutputTruncation =
  /** The final output was cut: its full UTF-8 size and sha256, as recorded. */
  | { kind: "final"; bytes: number | null; sha256: string | null }
  /** Streamed output stopped at the cap, and no final output is recorded yet. */
  | { kind: "stream" }

/** How the recorded output of a tool call falls short of the whole output, from its events; null when it does not. */
export function outputTruncation(events: readonly TrajectoryEvent[] | undefined): OutputTruncation | null {
  let streamed = false
  for (const event of events ?? []) {
    if (event.type !== "tool.output") continue
    const data = event.data ?? {}
    if (data.stage === "executor_result") {
      // The final result is the call's last output and replaces what was streamed.
      if (data.output_truncated !== true) return null
      return {
        kind: "final",
        bytes: typeof data.output_bytes === "number" ? data.output_bytes : null,
        sha256: typeof data.output_sha256 === "string" ? data.output_sha256 : null,
      }
    }
    if (data.stream_truncated === true) streamed = true
  }
  return streamed ? { kind: "stream" } : null
}
