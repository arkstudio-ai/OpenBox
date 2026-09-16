// The read budget of a viewer (SPEC §11.2): these intervals decide how much
// load an open trajectory page puts on the server, so they are pinned here.
import { describe, expect, it } from "vitest"
import {
  EVENTS_POLL_CONNECTED_MS,
  EVENTS_POLL_DISCONNECTED_MS,
  EVENTS_POLL_HIDDEN_MS,
  eventPollDelay,
  HEADER_HINT_MIN_MS,
  HEADER_REFRESH_CONNECTED_MS,
  HEADER_REFRESH_DISCONNECTED_MS,
  headerRefreshDelay,
  LIST_PROBE_CONNECTED_MS,
  LIST_PROBE_DISCONNECTED_MS,
  listProbeDelay,
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
      HEADER_REFRESH_CONNECTED_MS,
      HEADER_REFRESH_DISCONNECTED_MS,
      HEADER_HINT_MIN_MS,
      LIST_PROBE_CONNECTED_MS,
      LIST_PROBE_DISCONNECTED_MS,
      RECORD_DETAIL_SETTLE_MS,
      PAYLOAD_REVALIDATE_MS,
      PAYLOAD_META_REVALIDATE_MS,
    }).toEqual({
      EVENTS_POLL_CONNECTED_MS: 30_000,
      EVENTS_POLL_DISCONNECTED_MS: 2_000,
      EVENTS_POLL_HIDDEN_MS: 5_000,
      HEADER_REFRESH_CONNECTED_MS: 60_000,
      HEADER_REFRESH_DISCONNECTED_MS: 30_000,
      HEADER_HINT_MIN_MS: 5_000,
      LIST_PROBE_CONNECTED_MS: 60_000,
      LIST_PROBE_DISCONNECTED_MS: 30_000,
      RECORD_DETAIL_SETTLE_MS: 2_000,
      PAYLOAD_REVALIDATE_MS: 15_000,
      PAYLOAD_META_REVALIDATE_MS: 60_000,
    })
  })

  it("keeps an idle viewer with an open socket within 4 safety reads a minute", () => {
    const perMinute = (ms: number) => 60_000 / ms
    const events = perMinute(eventPollDelay(true, false))
    const header = perMinute(headerRefreshDelay(true))
    const list = perMinute(listProbeDelay(true))
    expect(events + header + list).toBeLessThanOrEqual(4)
  })

  it("polls events slowly while hints arrive, quickly while they cannot, and never fast in a hidden tab", () => {
    expect(eventPollDelay(true, false)).toBe(30_000)
    expect(eventPollDelay(false, false)).toBe(2_000)
    expect(eventPollDelay(false, true)).toBe(5_000)
    expect(eventPollDelay(true, true)).toBe(30_000)
  })

  it("reads the header and the list probe half as often while hints arrive", () => {
    expect(headerRefreshDelay(true)).toBe(60_000)
    expect(headerRefreshDelay(false)).toBe(30_000)
    expect(listProbeDelay(true)).toBe(60_000)
    expect(listProbeDelay(false)).toBe(30_000)
  })
})
