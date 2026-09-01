// The video model the user has selected in the composer but not yet sent with.
//
// A sibling of model-choice.ts rather than a field inside it: the two are
// picked independently, and merging them would make every chat-model switch
// look like a video-model switch to anything watching the store.
//
// Keyed by session for the same reason as the chat model — an unsent choice
// belongs to the conversation it was made in.
import { create } from "zustand"

/** A model and the resolution chosen with it. The pair travels together
 *  because one does not mean anything without the other: the same 720p costs
 *  and looks different per model, and a model's tiers are its own. */
export interface VideoPick {
  modelId: string
  resolution: string
}

interface VideoModelChoiceState {
  picked: Map<string, VideoPick>
  pick: (sessionKey: string, choice: VideoPick) => void
  /** Drop the pick for one conversation (it has been sent, or abandoned). */
  clear: (sessionKey: string) => void
}

export const useVideoModelChoiceStore = create<VideoModelChoiceState>((set) => ({
  picked: new Map(),
  pick: (sessionKey, choice) =>
    set((s) => {
      const next = new Map(s.picked)
      next.set(sessionKey, choice)
      return { picked: next }
    }),
  clear: (sessionKey) =>
    set((s) => {
      if (!s.picked.has(sessionKey)) return s
      const next = new Map(s.picked)
      next.delete(sessionKey)
      return { picked: next }
    }),
}))

/** The unsent video-model choice for a conversation, if the user made one.
 *
 *  Returns `undefined` rather than a default, like its chat-model counterpart:
 *  absent means "leave whatever the session already records". Substituting a
 *  default here would pin every turn to a model the user never chose — and for
 *  video that pin costs money.
 */
export function usePickedVideoModel(sessionKey: string | undefined): VideoPick | undefined {
  return useVideoModelChoiceStore((s) => (sessionKey ? s.picked.get(sessionKey) : undefined))
}
