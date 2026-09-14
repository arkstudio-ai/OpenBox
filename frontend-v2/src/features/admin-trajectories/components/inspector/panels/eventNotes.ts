import type { TrajectoryEvent } from "../../../types/protocol"

/** `raw_content_mode` values meaning raw chunk slots reference the sanitized blocks of the same event. */
const SANITIZED_RAW_MODES: ReadonlySet<unknown> = new Set([
  "sanitized_stream_references_and_complete_fields",
  "sanitized_block_references",
])

/**
 * Recorder redaction: raw chunk slots may hold `{$stream_blocks: [...]}`
 * references to the sanitized blocks of the same event instead of text.
 */
export function hasSanitizedRaw(events: readonly TrajectoryEvent[] | undefined): boolean {
  return !!events?.some((event) => SANITIZED_RAW_MODES.has(event.data?.raw_content_mode))
}

/** The recorder flushing buffered sanitized text at stream end — not a provider chunk. */
export function isFinalize(event: TrajectoryEvent): boolean {
  return event.data?.redaction_control === "finalize"
}
