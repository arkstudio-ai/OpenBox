import { useMemo } from "react"
import type { Position, SyncSnapshot } from "../../api/sync"
import { useTrajectoryView, type PlaybackRate } from "../../stores/view"
import type { ProjectionState, Seq, TrajectoryEvent } from "../../types/protocol"
import { nextEventSeq, previousEventSeq } from "../../utils/playback"
import { eqSeq, gtSeq, lastIndexAtOrBefore, lteSeq, ltSeq, sortBySeq } from "../../utils/seq"

export interface PlaybackModel {
  live: boolean
  playing: boolean
  loading: boolean
  /** The shown position: playhead in replay, loaded head when live. */
  current: Seq
  /** Earliest position replay can start from: the start of recorded history. */
  floor: Seq
  loadedSeq: Seq
  headSeq: Seq
  /** Committed events after the playhead (as a decimal string); "0" when live. */
  newer: string
  rate: PlaybackRate
  skipIdle: boolean
  previousEvent: Seq | null
  nextEvent: Seq | null
  previousStep: Seq | null
  nextStep: Seq | null
}

export interface PlaybackActions {
  togglePlay: () => void
  seek: (seq: Seq) => void
  setRate: (rate: PlaybackRate) => void
  setSkipIdle: (skipIdle: boolean) => void
  returnToLive: () => void
}

/**
 * Recorded history starts before seq 1 on every trajectory. The loaded
 * segment (a head checkpoint plus its tail) is only a buffer: positions before
 * it are fetched on demand by the sync engine (older checkpoint + tail).
 */
export const HISTORY_FLOOR: Seq = "0"

const STEP_EVENTS: ReadonlySet<string> = new Set(["step.started"])
const FALLBACK_EVENTS: ReadonlySet<string> = new Set(["turn.started", "request.started"])
const STEP_KINDS: ReadonlySet<string> = new Set(["step"])
const FALLBACK_KINDS: ReadonlySet<string> = new Set(["turn", "request"])

type Records = ProjectionState["records"] | undefined

function hasKind(records: Records, kinds: ReadonlySet<string>): boolean {
  return !!records && Object.values(records).some((record) => kinds.has(record.kind))
}

/** Ascending, de-duplicated boundary seqs from records of the chosen kinds. */
function recordStarts(sources: readonly Records[], kinds: ReadonlySet<string>): Seq[] {
  const starts = new Set<Seq>()
  for (const records of sources) {
    if (!records) continue
    for (const record of Object.values(records)) if (kinds.has(record.kind)) starts.add(record.start_seq)
  }
  return sortBySeq([...starts], (seq) => seq)
}

function eventStarts(events: readonly TrajectoryEvent[], types: ReadonlySet<string>): Seq[] {
  return events.filter((event) => types.has(event.type)).map((event) => event.seq)
}

/** Nearest boundary strictly after `current` and not past `ceiling`, across ascending lists. */
function nextBoundary(lists: readonly (readonly Seq[])[], current: Seq, ceiling: Seq): Seq | null {
  let best: Seq | null = null
  for (const list of lists) {
    const candidate = list[lastIndexAtOrBefore(list, current, (seq) => seq) + 1]
    if (candidate !== undefined && lteSeq(candidate, ceiling) && (best === null || ltSeq(candidate, best)))
      best = candidate
  }
  return best
}

/** Nearest boundary strictly before `current`, across ascending lists. */
function previousBoundary(lists: readonly (readonly Seq[])[], current: Seq): Seq | null {
  let best: Seq | null = null
  for (const list of lists) {
    let index = lastIndexAtOrBefore(list, current, (seq) => seq)
    if (index >= 0 && eqSeq(list[index], current)) index -= 1
    const candidate = list[index]
    if (candidate !== undefined && (best === null || gtSeq(candidate, best))) best = candidate
  }
  return best
}

/**
 * Step boundaries used only to choose a target seq; the target state is always
 * rebuilt by the sync engine. A checkpoint carries historic Step records but
 * not their events, so after a seek the loaded events may miss Steps that the
 * loaded projection (records through `loadedSeq`) and the position still know.
 * One boundary type is used everywhere: Steps if any source has them, otherwise
 * turn/request starts.
 */
function useStepIndex(snapshot: SyncSnapshot, position: Position | null) {
  const { events, live } = snapshot
  const shown = position?.status === "ready" ? position.state.records : undefined
  const steps = useMemo(
    () =>
      hasKind(live?.records, STEP_KINDS) ||
      hasKind(shown, STEP_KINDS) ||
      events.some((event) => STEP_EVENTS.has(event.type)),
    [events, live, shown],
  )
  const fromEvents = useMemo(
    () => eventStarts(events, steps ? STEP_EVENTS : FALLBACK_EVENTS),
    [events, steps],
  )
  const fromLoaded = useMemo(
    () => recordStarts([live?.records], steps ? STEP_KINDS : FALLBACK_KINDS),
    [live, steps],
  )
  const fromPosition = useMemo(
    () => recordStarts([shown], steps ? STEP_KINDS : FALLBACK_KINDS),
    [shown, steps],
  )
  return { fromEvents, fromLoaded, fromPosition }
}

/** Read-only replay navigation over committed events: it moves the position, never the recorded session. */
export function usePlaybackControls(
  snapshot: SyncSnapshot,
  position: Position | null,
): { model: PlaybackModel; actions: PlaybackActions } {
  const playhead = useTrajectoryView((s) => s.playhead)
  const playing = useTrajectoryView((s) => s.playing)
  const rate = useTrajectoryView((s) => s.rate)
  const skipIdle = useTrajectoryView((s) => s.skipIdle)
  const setPlayhead = useTrajectoryView((s) => s.setPlayhead)
  const setPlaying = useTrajectoryView((s) => s.setPlaying)
  const setRate = useTrajectoryView((s) => s.setRate)
  const setSkipIdle = useTrajectoryView((s) => s.setSkipIdle)
  const returnToLive = useTrajectoryView((s) => s.returnToLive)

  const { loadedSeq, headSeq } = snapshot
  const current = playhead ?? loadedSeq
  const { fromEvents, fromLoaded, fromPosition } = useStepIndex(snapshot, position)

  const model = useMemo<PlaybackModel>(
    () => ({
      live: playhead === null,
      playing,
      loading: position?.status === "loading",
      current,
      floor: HISTORY_FLOOR,
      loadedSeq,
      headSeq,
      newer:
        playhead === null || !ltSeq(playhead, loadedSeq)
          ? "0"
          : (BigInt(loadedSeq) - BigInt(playhead)).toString(),
      rate,
      skipIdle,
      previousEvent: previousEventSeq(current, HISTORY_FLOOR),
      nextEvent: nextEventSeq(current, loadedSeq),
      previousStep: previousBoundary([fromEvents, fromLoaded, fromPosition], current),
      nextStep: nextBoundary([fromEvents, fromLoaded], current, loadedSeq),
    }),
    [
      current,
      fromEvents,
      fromLoaded,
      fromPosition,
      headSeq,
      loadedSeq,
      playhead,
      playing,
      position?.status,
      rate,
      skipIdle,
    ],
  )

  const actions = useMemo<PlaybackActions>(
    () => ({
      togglePlay: () => {
        if (playing) {
          setPlaying(false)
          return
        }
        // At the head there is nothing left to play: start again from the beginning of history.
        if (playhead === null || !ltSeq(playhead, loadedSeq)) setPlayhead(HISTORY_FLOOR)
        setPlaying(true)
      },
      seek: (seq) => {
        const clamped = gtSeq(seq, loadedSeq) ? loadedSeq : ltSeq(seq, HISTORY_FLOOR) ? HISTORY_FLOOR : seq
        if (playhead === null || !eqSeq(playhead, clamped)) setPlayhead(clamped)
      },
      setRate,
      setSkipIdle,
      returnToLive,
    }),
    [loadedSeq, playhead, playing, returnToLive, setPlayhead, setPlaying, setRate, setSkipIdle],
  )

  return { model, actions }
}
