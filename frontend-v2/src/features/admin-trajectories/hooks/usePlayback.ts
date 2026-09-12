import { useEffect, useMemo } from "react"
import type { Position, SyncSnapshot, TrajectorySync } from "../api/sync"
import { useTrajectoryView } from "../stores/view"
import type { Seq } from "../types/protocol"
import { eventAt, nextEventSeq, playbackDelay } from "../utils/playback"
import { ltSeq } from "../utils/seq"

/**
 * The projection at the effective position: the playhead when replaying, the
 * loaded head when following live. Missing history is fetched from an effect
 * (checkpoint or start, plus tail) and the view shows "loading position" —
 * never the live state — until it is ready.
 */
export function usePosition(sync: TrajectorySync | null, snapshot: SyncSnapshot | null): Position | null {
  const playhead = useTrajectoryView((s) => s.playhead)
  const playing = useTrajectoryView((s) => s.playing)
  const setPlayhead = useTrajectoryView((s) => s.setPlayhead)
  const seq: Seq | null = snapshot ? (playhead ?? snapshot.loadedSeq) : null
  const phase = snapshot?.phase
  // The engine emits a new snapshot object whenever it learns something, so the
  // snapshot identity is what invalidates a position; stateAt itself is memoised
  // per segment and returns the same state object when nothing relevant moved.
  const position = useMemo(
    () => (sync && snapshot && seq !== null ? sync.stateAt(seq) : null),
    [sync, snapshot, seq],
  )
  // A pinned position is already "loading" while the engine opens, when a
  // request for older history is ignored; the phase change asks again.
  useEffect(() => {
    if (sync && seq !== null && phase === "live" && position?.status === "loading") sync.ensurePosition(seq)
  }, [phase, position?.status, seq, sync])

  const ahead = playing && playhead !== null ? unloadedNext(snapshot, position) : null
  useEffect(() => {
    if (!sync || ahead === null) return
    // A failed read ahead shows where playback stopped, like a failed seek.
    if (sync.stateAt(ahead).status === "error") setPlayhead(ahead)
    else sync.ensurePosition(ahead)
  }, [ahead, setPlayhead, snapshot, sync])
  return position
}

/**
 * Older history is read only through the position shown, but playback paces a
 * step by the next event's recorded time — so that one event is read first.
 */
function unloadedNext(snapshot: SyncSnapshot | null, position: Position | null): Seq | null {
  if (!snapshot || position?.status !== "ready") return null
  const next = nextEventSeq(position.seq, snapshot.loadedSeq)
  return next !== null && ltSeq(next, snapshot.baseSeq) && !eventAt(snapshot.events, next) ? next : null
}

/** Advances the playhead event by event while playing, paced by recorded time. */
export function usePlaybackTimer(snapshot: SyncSnapshot | null, position: Position | null): void {
  const playing = useTrajectoryView((s) => s.playing)
  const playhead = useTrajectoryView((s) => s.playhead)
  const rate = useTrajectoryView((s) => s.rate)
  const skipIdle = useTrajectoryView((s) => s.skipIdle)
  const setPlayhead = useTrajectoryView((s) => s.setPlayhead)
  const setPlaying = useTrajectoryView((s) => s.setPlaying)
  const events = snapshot?.events
  const loadedSeq = snapshot?.loadedSeq
  const baseSeq = snapshot?.baseSeq

  useEffect(() => {
    if (!playing || !events || !loadedSeq || playhead === null) return
    if (position?.status !== "ready") return
    const next = nextEventSeq(playhead, loadedSeq)
    if (next === null) {
      setPlaying(false)
      return
    }
    const nextEvent = eventAt(events, next)
    // usePosition is reading it; the new snapshot re-runs this with its time.
    if (!nextEvent && baseSeq && ltSeq(next, baseSeq)) return
    const delay = nextEvent ? playbackDelay(eventAt(events, playhead), nextEvent, { rate, skipIdle }) : 0
    const timer = window.setTimeout(() => setPlayhead(next), delay)
    return () => window.clearTimeout(timer)
  }, [
    baseSeq,
    events,
    loadedSeq,
    playhead,
    playing,
    position?.status,
    rate,
    setPlayhead,
    setPlaying,
    skipIdle,
  ])
}
