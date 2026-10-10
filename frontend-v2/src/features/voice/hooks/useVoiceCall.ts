import { useEffect, type RefObject } from "react"
import { useShallow } from "zustand/react/shallow"
import { useLiveElapsed } from "@/shared/hooks/useLiveElapsed"
import { isNormalEnding } from "../lib/endings"
import { displayPhase } from "../lib/reducer"
import type { CallEnd, CallStatus } from "../lib/types"
import { useVoiceStore } from "../store"

/** How long a normal ending's panel stays before closing itself. */
const AUTO_DISMISS_MS = 8_000

/** The call as the window and the entry button use it, with its actions. Level
 *  readings are left out on purpose: they change twenty times a second. */
export function useVoiceCall() {
  return useVoiceStore(
    useShallow((state) => ({
      status: state.call.status,
      phase: displayPhase(state.call),
      working: state.call.working,
      late: state.call.late,
      startedAt: state.call.startedAt,
      stoppedAt: state.call.stoppedAt,
      maxSeconds: state.call.maxSeconds,
      cost: state.call.cost,
      ended: state.call.ended,
      expanded: state.expanded,
      muted: state.muted,
      start: state.start,
      hangUp: state.hangUp,
      toggleMute: state.toggleMute,
      setExpanded: state.setExpanded,
      dismiss: state.dismiss,
    })),
  )
}

/** Milliseconds on the call clock: running while connected, stopped at the hang-up. */
export function useCallElapsed(
  status: CallStatus,
  startedAt: number | null,
  stoppedAt: number | null,
): number {
  const live = useLiveElapsed(
    startedAt === null ? undefined : new Date(startedAt).toISOString(),
    status === "connected",
  )
  if (startedAt === null) return 0
  return stoppedAt === null ? live : stoppedAt - startedAt
}

/** Keep an element's `--level` on the live loudness of `source`, without re-rendering anything. */
export function useLevelVariable(ref: RefObject<HTMLElement | null>, source: "mic" | "out" | null): void {
  useEffect(() => {
    const element = ref.current
    if (!element) return
    const show = (level: { mic: number; out: number }) =>
      element.style.setProperty("--level", String(source === null ? 0 : level[source]))
    show(useVoiceStore.getState().call.level)
    return useVoiceStore.subscribe((state, previous) => {
      if (state.call.level !== previous.call.level) show(state.call.level)
    })
  }, [ref, source])
}

/** A normal ending's panel closes itself; a failure stays until it has been read. */
export function useAutoDismiss(ended: CallEnd | null, dismiss: () => void): void {
  useEffect(() => {
    if (!ended || !isNormalEnding(ended.reason)) return
    const timer = window.setTimeout(dismiss, AUTO_DISMISS_MS)
    return () => window.clearTimeout(timer)
  }, [ended, dismiss])
}
