// The read budget of a viewer (SPEC §11.2): these intervals decide how much
// load an open trajectory page puts on the server, so they are pinned here.
import { describe, expect, it } from "vitest"
import {
  EVENTS_POLL_CONNECTED_MS,
  EVENTS_POLL_DISCONNECTED_MS,
  EVENTS_POLL_HIDDEN_MS,
  eventPollDelay,
  HEADER_HINT_MIN_MS,
  HEADER_REFRESH_MS,
  LIST_PROBE_MS,
  PAYLOAD_META_REVALIDATE_MS,
  PAYLOAD_REVALIDATE_MS,
  RECORD_DETAIL_SETTLE_MS,
} from "./polling"

describe("polling budget", () => {
  it("keeps the agreed intervals", () => {
    expect({
      EVENTS_POLL_CONNECTED_MS,
      EVENTS_POLL_DISCONNECTED_MS,
      EVENTS_POLL_HIDDEN_MS,
      HEADER_REFRESH_MS,
      HEADER_HINT_MIN_MS,
      LIST_PROBE_MS,
      RECORD_DETAIL_SETTLE_MS,
      PAYLOAD_REVALIDATE_MS,
      PAYLOAD_META_REVALIDATE_MS,
    }).toEqual({
      EVENTS_POLL_CONNECTED_MS: 10_000,
      EVENTS_POLL_DISCONNECTED_MS: 2_000,
      EVENTS_POLL_HIDDEN_MS: 5_000,
      HEADER_REFRESH_MS: 30_000,
      HEADER_HINT_MIN_MS: 5_000,
      LIST_PROBE_MS: 30_000,
      RECORD_DETAIL_SETTLE_MS: 2_000,
      PAYLOAD_REVALIDATE_MS: 15_000,
      PAYLOAD_META_REVALIDATE_MS: 60_000,
    })
  })

  it("polls events slowly while hints arrive, quickly while they cannot, and never fast in a hidden tab", () => {
    expect(eventPollDelay(true, false)).toBe(10_000)
    expect(eventPollDelay(false, false)).toBe(2_000)
    expect(eventPollDelay(false, true)).toBe(5_000)
    expect(eventPollDelay(true, true)).toBe(10_000)
  })
})
