import type { TrajectoryEvent } from "../../../types/protocol"

/** `raw_content_mode` values meaning raw chunk slots reference the blocks of the same event. */
const STREAM_REFERENCE_MODES: ReadonlySet<unknown> = new Set(["stream_references"])

/**
 * The recorder keeps each streamed text once: raw chunk slots may hold
 * `{$stream_blocks: [...]}` references to the blocks of the same event instead of text.
 */
export function hasStreamReferences(events: readonly TrajectoryEvent[] | undefined): boolean {
  return !!events?.some((event) => STREAM_REFERENCE_MODES.has(event.data?.raw_content_mode))
}
