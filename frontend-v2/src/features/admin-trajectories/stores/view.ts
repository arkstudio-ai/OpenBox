// View state of the session inspector: which position is shown, what is
// selected, how playback runs. Server data never lives here (§7.2) — records
// come from the sync engine's projection or from Query. Changing the target
// session resets everything, so one person's selection or playhead can never
// be applied to another person's session.
import { create } from "zustand"
import type { Seq } from "../types/protocol"

export type TimeMode = "local" | "utc" | "unix"
export type TimelineScale = "sequence" | "duration"

export const PLAYBACK_RATES = [0.5, 1, 2, 4, 8] as const
export type PlaybackRate = (typeof PLAYBACK_RATES)[number]

export const INSPECTOR_MIN = 320
export const INSPECTOR_MAX = 880

interface ViewState {
  targetKey: string | null
  /** null follows the live head; a seq pins the replay position. */
  playhead: Seq | null
  selectedRecordId: string | null
  collapsed: Readonly<Record<string, true>>
  playing: boolean
  rate: PlaybackRate
  skipIdle: boolean
  timeMode: TimeMode
  timelineScale: TimelineScale
  inspectorWidth: number
  bindTarget: (targetKey: string, initial?: { playhead?: Seq | null; record?: string | null }) => void
  setPlayhead: (seq: Seq | null) => void
  returnToLive: () => void
  select: (recordId: string | null) => void
  toggleCollapsed: (groupId: string) => void
  setPlaying: (playing: boolean) => void
  setRate: (rate: PlaybackRate) => void
  setSkipIdle: (skipIdle: boolean) => void
  cycleTimeMode: () => void
  setTimelineScale: (scale: TimelineScale) => void
  setInspectorWidth: (width: number) => void
  reset: () => void
}

const TIME_MODES: readonly TimeMode[] = ["local", "utc", "unix"]

const initial = {
  targetKey: null,
  playhead: null,
  selectedRecordId: null,
  collapsed: {},
  playing: false,
  rate: 1 as PlaybackRate,
  skipIdle: true,
  timeMode: "local" as TimeMode,
  timelineScale: "sequence" as TimelineScale,
  inspectorWidth: 440,
}

export const useTrajectoryView = create<ViewState>((set, get) => ({
  ...initial,
  bindTarget: (targetKey, start) => {
    if (get().targetKey === targetKey) return
    set({
      ...initial,
      // Display preferences are the viewer's, not the target's; keep them.
      timeMode: get().timeMode,
      timelineScale: get().timelineScale,
      inspectorWidth: get().inspectorWidth,
      targetKey,
      playhead: start?.playhead ?? null,
      selectedRecordId: start?.record ?? null,
    })
  },
  setPlayhead: (seq) => set({ playhead: seq }),
  returnToLive: () => set({ playhead: null, playing: false }),
  select: (recordId) => set({ selectedRecordId: recordId }),
  toggleCollapsed: (groupId) =>
    set((state) => {
      const next = { ...state.collapsed }
      if (next[groupId]) delete next[groupId]
      else next[groupId] = true
      return { collapsed: next }
    }),
  setPlaying: (playing) => set({ playing }),
  setRate: (rate) => set({ rate }),
  setSkipIdle: (skipIdle) => set({ skipIdle }),
  cycleTimeMode: () =>
    set((state) => ({ timeMode: TIME_MODES[(TIME_MODES.indexOf(state.timeMode) + 1) % TIME_MODES.length] })),
  setTimelineScale: (timelineScale) => set({ timelineScale }),
  setInspectorWidth: (width) =>
    set({ inspectorWidth: Math.round(Math.min(INSPECTOR_MAX, Math.max(INSPECTOR_MIN, width))) }),
  reset: () => set({ ...initial }),
}))
