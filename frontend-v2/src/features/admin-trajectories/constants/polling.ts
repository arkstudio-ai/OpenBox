// How often the viewer re-reads what can change under it (SPEC §11.2). The
// watermark socket announces every commit, so reads that only recover a lost
// announcement poll slowly while it is open. Reads pinned to a watermark are
// immutable and never poll.

/** Event catch-up while the watermark socket is open: only recovers a hint that never arrived. */
export const EVENTS_POLL_CONNECTED_MS = 10_000
/** Event catch-up while the socket is down or reconnecting: the poll is the only signal. */
export const EVENTS_POLL_DISCONNECTED_MS = 2_000
/** A hidden tab polls events no more often than this; it reads at once when shown again. */
export const EVENTS_POLL_HIDDEN_MS = 5_000
/** The live header without a hint. */
export const HEADER_REFRESH_MS = 30_000
/** A streaming run announces every commit; the header follows at most this often. */
export const HEADER_HINT_MIN_MS = 5_000
/** The session list's "updated" probe. */
export const LIST_PROBE_MS = 30_000
/** Record details follow a moving position (live, playback) at most this often. */
export const RECORD_DETAIL_SETTLE_MS = 2_000
/** Shown protected content is re-read this often to notice a deletion, where the server has no availability check. */
export const PAYLOAD_REVALIDATE_MS = 15_000
/** Availability-only check (`?meta=1`) of shown protected content, where the server offers it. */
export const PAYLOAD_META_REVALIDATE_MS = 60_000

/** Delay before the next event catch-up read. */
export function eventPollDelay(connected: boolean, hidden: boolean): number {
  const delay = connected ? EVENTS_POLL_CONNECTED_MS : EVENTS_POLL_DISCONNECTED_MS
  return hidden ? Math.max(delay, EVENTS_POLL_HIDDEN_MS) : delay
}
