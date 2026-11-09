// A voice call hands the user's words to the assistant (`turn{accepted}` with
// `text`). Their message is only written when the assistant claims the turn,
// which waits for the turn before it to end; until then the earlier reply
// would land above these words. So the assistant's conversation shows them
// at once as a pending bubble under the client id the server's message will
// carry, and the store's echo rules put that message in its place.
import { useEffect } from "react"
import { onAppEvent } from "@/shared/events/bus"
import { optimisticUserMessage } from "../lib/message"
import { useStreamStore } from "../stores/stream"

/** A bubble whose turn the assistant never claimed (the call failed first) must not stay forever. */
const ECHO_TTL_MS = 15 * 60_000

export function useVoiceInputEcho(sessionId: string, enabled: boolean): void {
  useEffect(() => {
    if (!enabled) return
    const timers = new Set<number>()
    const stop = onAppEvent("chat.voice_input", ({ clientMessageId, text }) => {
      const store = useStreamStore.getState()
      const held = store.messages.get(sessionId) ?? []
      if (held.some((m) => m.client_message_id === clientMessageId)) return
      store.addMessage(sessionId, optimisticUserMessage(sessionId, text, clientMessageId))
      const timer = window.setTimeout(() => {
        timers.delete(timer)
        useStreamStore.getState().dropOptimistic(sessionId, clientMessageId)
      }, ECHO_TTL_MS)
      timers.add(timer)
    })
    return () => {
      stop()
      timers.forEach((timer) => window.clearTimeout(timer))
    }
  }, [sessionId, enabled])
}
