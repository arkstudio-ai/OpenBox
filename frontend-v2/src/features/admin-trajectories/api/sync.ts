// Ordered catch-up for one target session. The socket and the periodic poll only
// say "something may be committed"; every fact arrives through REST event
// pages, applied strictly in seq order. Duplicates and stale watermarks are
// ignored, a hole in the sequence is reported instead of skipped, and any
// response that lands after the target changed is dropped by generation.
//
// Why this is not TanStack Query: events are a streaming log applied
// incrementally (ENGINEERING_SPEC §7.4). Query owns server snapshots — header,
// record pages, details, search, payloads — keyed by viewer + target + H. The
// engine owns only the ordered event stream and the projection folded from it;
// checkpoints are installed here solely as the base of that fold (open/seek)
// and are never read back through Query, so there is one source per fact.
//
// The live projection (head) and replay positions (playhead) are separate: a
// position is rebuilt from a compatible checkpoint plus its tail, or from the
// start when no checkpoint fits, and never from the live state.
import { ApiError } from "@/shared/api/http"
import type { CheckpointResponse, EventPage, ProjectionState, Seq, TrajectoryEvent } from "../types/protocol"
import { EventShapeError, ingestCheckpoint, validateEvent, type CheckpointRejection } from "../utils/adapter"
import { emptyState, reduceMany } from "../utils/projector"
import { addSeq, eqSeq, gtSeq, lastIndexAtOrBefore, lteSeq, ltSeq, maxSeq, minSeq } from "../utils/seq"
import type { EventPageParams } from "./endpoints"

export interface SyncTransport {
  events(params: EventPageParams, signal: AbortSignal): Promise<EventPage>
  checkpoint(atSeq: Seq | undefined, signal: AbortSignal): Promise<CheckpointResponse>
}

/**
 * `gone`: the session or its trajectory was deleted after it was shown; every
 * loaded body has been dropped. `denied`: the viewer lost access; likewise.
 */
export type SyncPhase = "idle" | "opening" | "live" | "stopped" | "denied" | "gone"

export type SyncErrorKind =
  "denied" | "not_recorded" | "deleted" | "corrupt" | "gap" | "malformed" | "too_large" | "network"

export interface SyncError {
  kind: SyncErrorKind
  status?: number
  /** First seq that could not be applied, when known. */
  seq?: Seq
}

/** Where a segment's base state came from. `start_fallback`: a checkpoint existed but was refused. */
export type BaseOrigin = "checkpoint" | "start" | "start_fallback"

interface Segment {
  base: ProjectionState
  origin: BaseOrigin
  rejection: CheckpointRejection | null
  /** Contiguous events after `base.through_seq`. */
  events: TrajectoryEvent[]
}

export interface SyncSnapshot {
  phase: SyncPhase
  /** Projection at `loadedSeq`; null until the base is installed and after any wipe. */
  live: ProjectionState | null
  loadedSeq: Seq
  /** Newest committed seq the server has reported; may run ahead of `loadedSeq`. */
  headSeq: Seq
  baseSeq: Seq
  origin: BaseOrigin | null
  /** Why a checkpoint was refused (the replay then started from zero). Visible, never silent. */
  rejection: CheckpointRejection | null
  error: SyncError | null
  /** Every event known locally, ascending and gap-free within each segment. */
  events: readonly TrajectoryEvent[]
  /** Increments on every change, for memoising derived views. */
  version: number
}

export type Position =
  | { status: "ready"; seq: Seq; state: ProjectionState; origin: BaseOrigin }
  | { status: "loading"; seq: Seq }
  | { status: "error"; seq: Seq; error: SyncError }

export interface SyncOptions {
  pageSize?: number
  /** Pages per pump before yielding back to the UI. */
  maxPagesPerPump?: number
  onDenied?: (error: SyncError) => void
  /** The session disappeared (410, or 404 after it had loaded). */
  onGone?: (error: SyncError) => void
}

/**
 * How far past the replay segment a seek may extend it instead of fetching a
 * checkpoint. Stepping or playing through history then reads a few events per
 * step rather than the same large checkpoint again.
 */
const SIDE_EXTEND_PAGES = 4

/**
 * Least time from the end of one live event read to a read a watermark hint
 * starts. A streaming run announces every commit: the hints in between are
 * coalesced into one read on the trailing edge.
 */
export const HINT_READ_SPACING_MS = 300
/** A live read refused as busy or unavailable is retried this long after, when the answer names no Retry-After. */
export const DEFAULT_RETRY_AFTER_MS = 1_000

class GapError extends Error {
  constructor(readonly expected: Seq) {
    super(`Trajectory sequence gap at ${expected}`)
  }
}

/** An event claiming a position after the watermark its page was read at. */
class WatermarkError extends Error {
  constructor(readonly seq: Seq) {
    super(`Trajectory event ${seq} is beyond the requested watermark`)
  }
}

function isAbort(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError"
}

export function classifyError(error: unknown): SyncError {
  if (error instanceof ApiError) {
    if (error.status === 401 || error.status === 403) return { kind: "denied", status: error.status }
    if (error.status === 404) return { kind: "not_recorded", status: 404 }
    if (error.status === 410) return { kind: "deleted", status: 410 }
    if (error.status === 409) return { kind: "corrupt", status: 409 }
    if (error.status === 413) return { kind: "too_large", status: 413 }
    return { kind: "network", status: error.status }
  }
  if (error instanceof EventShapeError) return { kind: "malformed", seq: error.seq ?? undefined }
  if (error instanceof WatermarkError) return { kind: "malformed", seq: error.seq }
  if (error instanceof GapError) return { kind: "gap", seq: error.expected }
  return { kind: "network" }
}

/**
 * When to read again after a refusal the server marks as temporary — 429 (its
 * read slots are busy) or 503 (a read deadline, or the database) — from the
 * answer's Retry-After (delay-seconds or an HTTP date), never sooner than
 * HINT_READ_SPACING_MS. Null for any other failure.
 */
export function busyRetryDelay(error: unknown, now: number = Date.now()): number | null {
  if (!(error instanceof ApiError) || (error.status !== 429 && error.status !== 503)) return null
  const header = error.retryAfter?.trim() ?? ""
  let delay = DEFAULT_RETRY_AFTER_MS
  if (/^\d+$/.test(header)) {
    delay = Number(header) * 1000
  } else if (header) {
    const at = Date.parse(header)
    if (!Number.isNaN(at)) delay = at - now
  }
  return Math.max(HINT_READ_SPACING_MS, delay)
}

/** Events strictly after `after` (and not after `until`), verified contiguous. Throws on holes or bad envelopes. */
export function contiguousTail(raw: readonly unknown[], after: Seq, until?: Seq): TrajectoryEvent[] {
  const fresh: TrajectoryEvent[] = []
  let expected = addSeq(after, 1)
  for (const item of raw) {
    const event = validateEvent(item)
    if (until !== undefined && gtSeq(event.seq, until)) throw new WatermarkError(event.seq)
    if (lteSeq(event.seq, after) || (fresh.length && lteSeq(event.seq, fresh[fresh.length - 1].seq))) continue
    if (!eqSeq(event.seq, expected)) throw new GapError(expected)
    fresh.push(event)
    expected = addSeq(event.seq, 1)
  }
  return fresh
}

function segmentFrom(response: CheckpointResponse, atSeq: Seq): Segment {
  const result = ingestCheckpoint(response, atSeq)
  if (result.kind === "ok") return { base: result.state, origin: "checkpoint", rejection: null, events: [] }
  if (result.kind === "rejected")
    return { base: emptyState(), origin: "start_fallback", rejection: result.reason, events: [] }
  return { base: emptyState(), origin: "start", rejection: null, events: [] }
}

function lastSeq(segment: Segment): Seq {
  return segment.events.length ? segment.events[segment.events.length - 1].seq : segment.base.through_seq
}

function mergeEvents(
  before: readonly TrajectoryEvent[],
  after: readonly TrajectoryEvent[],
): TrajectoryEvent[] {
  if (!before.length) return [...after]
  const first = after[0]
  const head = first ? before.filter((event) => ltSeq(event.seq, first.seq)) : [...before]
  return [...head, ...after]
}

export class TrajectorySync {
  private generation = 0
  private controller = new AbortController()
  private main: Segment | null = null
  private live: ProjectionState | null = null
  private side: Segment | null = null
  private sideLoading: Seq | null = null
  private sideToken = 0
  private sideError: { seq: Seq; error: SyncError } | null = null
  private memos = new WeakMap<Segment, { index: number; state: ProjectionState }>()
  private head: Seq = "0"
  private opening = false
  private pumping = false
  private pumpAgain = false
  /** When the last live event read ended (`Date.now()`); a hint's read keeps HINT_READ_SPACING_MS after it. */
  private lastRead = 0
  /** A pump that is due: a hint's trailing read, or a retry the server asked for. */
  private timer: ReturnType<typeof setTimeout> | null = null
  /** The due pump honours a Retry-After: neither hints nor polls read before it. */
  private backoff = false
  private phase: SyncPhase = "idle"
  private error: SyncError | null = null
  private version = 0
  private snapshot: SyncSnapshot
  private readonly listeners = new Set<() => void>()
  private readonly pageSize: number
  private readonly maxPages: number

  constructor(
    private readonly transport: SyncTransport,
    private readonly options: SyncOptions = {},
  ) {
    this.pageSize = options.pageSize ?? 500
    this.maxPages = options.maxPagesPerPump ?? 20
    this.snapshot = this.buildSnapshot()
  }

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener)
    return () => this.listeners.delete(listener)
  }

  getSnapshot = (): SyncSnapshot => this.snapshot

  private buildSnapshot(): SyncSnapshot {
    const main = this.main
    const events = this.side && main ? mergeEvents(this.side.events, main.events) : (main?.events ?? [])
    return {
      phase: this.phase,
      live: this.live,
      loadedSeq: main ? lastSeq(main) : "0",
      headSeq: this.head,
      baseSeq: main?.base.through_seq ?? "0",
      origin: main?.origin ?? null,
      rejection: main?.rejection ?? null,
      error: this.error,
      events,
      version: this.version,
    }
  }

  private emit(): void {
    this.version += 1
    this.snapshot = this.buildSnapshot()
    for (const listener of this.listeners) listener()
  }

  private get terminal(): boolean {
    return this.phase === "stopped" || this.phase === "denied" || this.phase === "gone"
  }

  private async readBase(atSeq?: Seq): Promise<{ segment: Segment; through: Seq }> {
    try {
      const response = await this.transport.checkpoint(atSeq, this.controller.signal)
      return { segment: segmentFrom(response, atSeq ?? response.through_seq), through: response.through_seq }
    } catch (error) {
      if (!(error instanceof ApiError) || error.status !== 413) throw error
      // The snapshot is an optimization. Replay the same contiguous event log
      // in bounded pages; the first live page supplies the current watermark.
      return {
        segment: { base: emptyState(), origin: "start_fallback", rejection: "too_large", events: [] },
        through: atSeq ?? "0",
      }
    }
  }

  async open(): Promise<void> {
    if (this.opening || this.main || this.terminal) return
    const generation = this.generation
    this.opening = true
    this.phase = "opening"
    this.emit()
    try {
      const { segment, through } = await this.readBase()
      if (generation !== this.generation) return
      this.head = through
      this.main = segment
      this.live = this.main.base
      this.phase = "live"
      this.error = null
      this.opening = false
      this.emit()
      await this.pump()
    } catch (error) {
      if (generation === this.generation) this.fail(error)
    } finally {
      if (generation === this.generation) this.opening = false
    }
  }

  /**
   * A watermark hint from the socket or a status probe. Stale or duplicate
   * hints do nothing; the rest are coalesced into spaced reads (`requestPump`).
   */
  noteCommitted(seq: Seq): void {
    if (this.phase !== "live" || !this.main) return
    if (lteSeq(seq, lastSeq(this.main))) return
    this.head = maxSeq(this.head, seq)
    this.emit()
    this.requestPump()
  }

  /**
   * The periodic head check. Recovers a notification that was never delivered,
   * retries an open that failed on the network, and is how a deletion without
   * a new seq is noticed (the next read answers 404/410). It reads at once,
   * unless the server asked to wait: then its scheduled retry is the next read.
   */
  poll(): Promise<void> {
    if (this.backoff) return Promise.resolve()
    if (this.phase === "live") return this.pump()
    if (this.phase === "opening" && !this.main && !this.opening) return this.open()
    return Promise.resolve()
  }

  /** Stop and forget everything loaded. */
  stop(): void {
    this.error = null
    this.wipe("stopped")
  }

  private wipe(phase: SyncPhase): void {
    this.generation += 1
    this.controller.abort()
    this.controller = new AbortController()
    this.main = null
    this.live = null
    this.side = null
    this.sideLoading = null
    this.sideToken += 1
    this.sideError = null
    this.memos = new WeakMap()
    this.head = "0"
    this.opening = false
    this.pumpAgain = false
    this.cancelDue()
    this.phase = phase
    this.emit()
  }

  /**
   * Reads up to the committed head now. One already running is followed by
   * exactly one more (`pumpAgain`), spaced like a hint's read. A 429 or 503
   * does not fail the stream: the read is retried after the server's
   * Retry-After, and nothing reads before then.
   */
  private async pump(): Promise<void> {
    // The transport already reduced the page to one event. Retrying the same
    // oversized event cannot make progress; keep the prefix and explain why.
    if (this.error?.kind === "too_large") return
    if (this.pumping) {
      this.pumpAgain = true
      return
    }
    this.cancelDue()
    this.pumping = true
    this.pumpAgain = false
    const generation = this.generation
    let retry: number | null = null
    let failed = false
    try {
      let more = true
      while (more) more = await this.fetchTail(generation)
    } catch (error) {
      if (generation === this.generation) {
        retry = busyRetryDelay(error)
        failed = retry === null
        if (failed) this.fail(error)
      }
    } finally {
      this.pumping = false
    }
    if (generation !== this.generation) return
    if (retry !== null) {
      this.due(retry, true)
    } else if (this.pumpAgain && !failed) {
      this.pumpAgain = false
      this.requestPump()
    }
  }

  /**
   * A read for a hint: at once when the last live read ended at least
   * HINT_READ_SPACING_MS ago, otherwise on the trailing edge of that spacing.
   * However many hints arrive, a running pump records one more and a read that
   * is already due (trailing, or a retry) takes them all.
   */
  private requestPump(): void {
    if (this.pumping) {
      this.pumpAgain = true
      return
    }
    if (this.timer !== null) return
    const wait = this.lastRead + HINT_READ_SPACING_MS - Date.now()
    if (wait <= 0) void this.pump()
    else this.due(wait, false)
  }

  private due(delay: number, backoff: boolean): void {
    this.cancelDue()
    const generation = this.generation
    this.backoff = backoff
    this.timer = setTimeout(() => {
      this.timer = null
      this.backoff = false
      if (generation === this.generation) void this.pump()
    }, delay)
  }

  private cancelDue(): void {
    if (this.timer !== null) clearTimeout(this.timer)
    this.timer = null
    this.backoff = false
  }

  /** Event pages up to the committed head; true when the page budget ran out with more to read. */
  private async fetchTail(generation: number): Promise<boolean> {
    for (let pages = 0; pages < this.maxPages; pages += 1) {
      const main = this.main
      if (!main || generation !== this.generation) return false
      const after = lastSeq(main)
      const page = await this.transport
        .events({ afterSeq: after, limit: this.pageSize }, this.controller.signal)
        .finally(() => {
          this.lastRead = Date.now()
        })
      if (generation !== this.generation || this.main !== main) return false
      const fresh = contiguousTail(page.events, lastSeq(main), page.until_seq)
      this.head = maxSeq(this.head, maxSeq(page.committed_seq, page.until_seq))
      if (fresh.length) {
        main.events.push(...fresh)
        this.live = reduceMany(this.live ?? main.base, fresh)
      }
      if (this.error) this.error = null
      this.emit()
      // No `until` was sent, so an exhausted page means the server's committed
      // head at answer time is loaded. A newer hint pumps again.
      if (!page.has_more) return false
    }
    return true
  }

  private fail(error: unknown): void {
    if (isAbort(error)) return
    const classified = classifyError(error)
    this.error = classified
    if (classified.kind === "denied") {
      this.wipe("denied")
      this.options.onDenied?.(classified)
    } else if (classified.kind === "deleted" || (classified.kind === "not_recorded" && this.main)) {
      // Current deletion overrides history: nothing of the session stays on screen.
      this.wipe("gone")
      this.options.onGone?.(classified)
    } else if (classified.kind === "not_recorded") {
      this.phase = "stopped"
      this.emit()
    } else {
      // Corrupt, gapped or unreachable: keep the verified prefix and say what is wrong.
      this.emit()
    }
  }

  /* ------------------------------ positions ------------------------------ */

  /**
   * The projection at `seq`, if it can be built from what is loaded. Pure read:
   * call `ensurePosition` from an effect to fetch what is missing.
   */
  stateAt(seq: Seq): Position {
    const main = this.main
    if (!main || !this.live) return { status: "loading", seq }
    const loaded = lastSeq(main)
    const target = gtSeq(seq, loaded) ? loaded : seq
    if (eqSeq(target, loaded)) return { status: "ready", seq: target, state: this.live, origin: main.origin }
    if (lteSeq(main.base.through_seq, target)) {
      return { status: "ready", seq: target, state: this.project(main, target), origin: main.origin }
    }
    const side = this.side
    if (side && lteSeq(side.base.through_seq, target) && lteSeq(target, lastSeq(side))) {
      return { status: "ready", seq: target, state: this.project(side, target), origin: side.origin }
    }
    if (this.sideError && eqSeq(this.sideError.seq, target))
      return { status: "error", seq: target, error: this.sideError.error }
    return { status: "loading", seq: target }
  }

  ensurePosition(seq: Seq, { readAhead = false }: { readAhead?: boolean } = {}): void {
    if (this.stateAt(seq).status !== "loading" || !this.main || this.phase !== "live") return
    // Playback consumes nearby events in order. Fetch at most one page ahead,
    // stopping before the installed checkpoint; stateAt still folds only
    // through the displayed position. A manual seek reads exactly its target.
    const until = readAhead
      ? minSeq(addSeq(seq, this.pageSize - 1), addSeq(this.main.base.through_seq, -1))
      : seq
    void this.loadSide(seq, until)
  }

  private project(segment: Segment, seq: Seq): ProjectionState {
    const end = lastIndexAtOrBefore(segment.events, seq, (event) => event.seq) + 1
    const memo = this.memos.get(segment)
    const start = memo && memo.index <= end ? memo : { index: 0, state: segment.base }
    const state = reduceMany(start.state, segment.events.slice(start.index, end))
    this.memos.set(segment, { index: end, state })
    return state
  }

  /**
   * Events through `until`: the target for a seek, or a bounded page for
   * playback. A short step forward extends the
   * installed replay segment on its own base; anything else starts from the
   * checkpoint at or before `seq`. Only the most recent seek may install its
   * result, and it installs a new segment, so a superseded read never touches
   * the one on screen.
   */
  private async loadSide(seq: Seq, until: Seq): Promise<void> {
    if (this.sideLoading && eqSeq(this.sideLoading, seq)) return
    if (!this.main) return
    const generation = this.generation
    const token = ++this.sideToken
    const current = () => generation === this.generation && token === this.sideToken
    this.sideLoading = seq
    try {
      const reused = this.extendable(seq)
      let base = reused
      if (!base) {
        const { segment } = await this.readBase(seq)
        if (!current()) return
        base = segment
      }
      const fresh = await this.readThrough(lastSeq(base), until, current)
      if (!fresh) return
      const segment: Segment = { ...base, events: base.events.concat(fresh) }
      // Same base and same leading events: the fold so far still holds.
      const memo = reused && this.memos.get(reused)
      if (memo) this.memos.set(segment, memo)
      this.side = segment
      this.sideError = null
      this.sideLoading = null
      this.emit()
    } catch (error) {
      if (!current() || isAbort(error)) return
      this.sideLoading = null
      const classified = classifyError(error)
      if (classified.kind === "denied" || classified.kind === "deleted") {
        this.fail(error)
        return
      }
      this.sideError = { seq, error: classified }
      this.emit()
    }
  }

  /** The installed replay segment when `seq` is at most a few pages past its end. */
  private extendable(seq: Seq): Segment | null {
    const side = this.side
    if (!side || lteSeq(seq, lastSeq(side))) return null
    return lteSeq(seq, addSeq(lastSeq(side), this.pageSize * SIDE_EXTEND_PAGES)) ? side : null
  }

  /** Contiguous events after `after` through exactly `until`; null once superseded. */
  private async readThrough(
    after: Seq,
    until: Seq,
    current: () => boolean,
  ): Promise<TrajectoryEvent[] | null> {
    const fresh: TrajectoryEvent[] = []
    let last = after
    while (ltSeq(last, until)) {
      const page = await this.transport.events(
        { afterSeq: last, untilSeq: until, limit: this.pageSize },
        this.controller.signal,
      )
      if (!current()) return null
      const events = contiguousTail(page.events, last, until)
      for (const event of events) fresh.push(event)
      if (events.length) last = events[events.length - 1].seq
      // The position needs every event through `until`; a short answer is a hole, not a shorter position.
      if (!events.length || (!page.has_more && ltSeq(last, until))) throw new GapError(addSeq(last, 1))
    }
    return fresh
  }
}
