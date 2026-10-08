// The one call in this tab (docs/VOICE_CALL_WEB.md §7). Module-level, so a call
// survives route changes, drawers and settings pages. The session does the
// work; this keeps what the window shows and the call's latency marks.
import { create } from "zustand"
import { useAuthStore } from "@/shared/api/auth-store"
import { env } from "@/shared/config/env"
import { callReducer, initialCall, isLive, type CallAction } from "./lib/reducer"
import { VoiceCallSession, type SessionSink } from "./lib/session"
import type { CallState } from "./lib/types"

export interface TimingMark {
  mark: string
  /** performance.now() */
  t: number
}

interface VoiceStore {
  call: CallState
  /** The window's form, card or pill. Local only: the server never hears of it. */
  expanded: boolean
  muted: boolean
  /** Latency marks of the current or last call. */
  timings: TimingMark[]
  start: () => void
  hangUp: () => void
  toggleMute: () => void
  setExpanded: (expanded: boolean) => void
  /** Close the ended panel. */
  dismiss: () => void
}

declare global {
  interface Window {
    /** Dev and QA only, for latency scripts: see `debugMirrorEnabled`. */
    __openboxVoice?: { timings: TimingMark[]; state: CallState & { expanded: boolean; muted: boolean } }
  }
}

const DEBUG_FLAG = "openbox:voice-debug"

function debugMirrorEnabled(): boolean {
  if (env.dev) return true
  try {
    return localStorage.getItem(DEBUG_FLAG) === "1"
  } catch {
    return false
  }
}

let session: VoiceCallSession | null = null
let mirror = false

export const useVoiceStore = create<VoiceStore>((set, get) => {
  const apply = (action: CallAction) =>
    set((state) => {
      const call = callReducer(state.call, action)
      return call === state.call ? state : { call }
    })
  // A finished call's stray callbacks must not touch the next one's state.
  const sinkFor = (isCurrent: () => boolean): SessionSink => ({
    dispatch: (action) => {
      if (isCurrent()) apply(action)
    },
    mark: (name, t = performance.now()) => {
      if (isCurrent()) set((state) => ({ timings: [...state.timings, { mark: name, t }] }))
    },
  })
  return {
    call: initialCall,
    expanded: true,
    muted: false,
    timings: [],
    start: () => {
      // One call at a time: asking again just brings the window back.
      if (isLive(get().call.status)) {
        set({ expanded: true })
        return
      }
      mirror = debugMirrorEnabled()
      set({ timings: [], muted: false, expanded: true })
      const next: VoiceCallSession = new VoiceCallSession(sinkFor(() => session === next))
      session = next
      next.start()
    },
    hangUp: () => session?.hangUp(),
    toggleMute: () => {
      const muted = !get().muted
      session?.setMuted(muted)
      set({ muted })
    },
    setExpanded: (expanded) => set({ expanded }),
    dismiss: () => apply({ type: "dismiss" }),
  }
})

useVoiceStore.subscribe((state) => {
  if (mirror)
    window.__openboxVoice = {
      timings: state.timings,
      state: { ...state.call, expanded: state.expanded, muted: state.muted },
    }
})

// A call belongs to the account that placed it: signing out or switching
// accounts must not leave the microphone open behind the login page.
useAuthStore.subscribe((auth, previous) => {
  if (auth.user?.id === previous.user?.id || !session) return
  const leaving = session
  session = null
  leaving.abandon()
  useVoiceStore.setState({ call: initialCall, muted: false })
})
