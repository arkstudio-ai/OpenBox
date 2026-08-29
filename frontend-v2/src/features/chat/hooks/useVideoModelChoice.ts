// Which video model the composer generates with.
//
// The same shape as useModelChoice, deliberately: a conversation carries its
// own video model, the backend records it on the session, and reopening one
// restores what it was last using.
//
// What differs is what a switch means. Changing the chat model retargets the
// next turn and nothing else. Changing the video model only reaches segments
// that have not been submitted yet — anything already generating keeps the
// model it started with, because the backend freezes the choice onto the
// segment at submission. So this hook never has to ask "is something running?";
// switching mid-production is always safe.
import { useEffect } from "react"
import { useVideoModelChoiceStore, usePickedVideoModel } from "../stores/video-model-choice"

interface Options {
  /** The video model this conversation last used, from the session record. */
  sessionVideoModel?: string
  /** Changes when the user moves to another conversation. */
  sessionKey?: string
  /** Deployment default, for a conversation that has not chosen yet. */
  fallback?: string
}

// Mirrors useModelChoice: the empty composer is a real selection scope even
// before the backend has assigned a session id, and the store treats
// `undefined` as "there is no conversation".
const NEW_SESSION_KEY = "__new_session__"

export function useVideoModelChoice({ sessionVideoModel, sessionKey, fallback }: Options) {
  const key = sessionKey ?? NEW_SESSION_KEY
  const picked = usePickedVideoModel(key)
  const pickInStore = useVideoModelChoiceStore((s) => s.pick)
  const clear = useVideoModelChoiceStore((s) => s.clear)

  // Leaving a conversation drops its unsent pick, on unmount rather than by
  // comparing keys during render — the store is shared, and a render-time
  // reset would wipe the pick made in whichever chat mounted next.
  useEffect(() => () => clear(key), [key, clear])

  return {
    activeId: picked ?? (sessionVideoModel || undefined) ?? fallback,
    /** What to send with the next turn: only an actual pick, never the default. */
    pending: picked,
    pick: (id: string) => pickInStore(key, id),
  }
}
