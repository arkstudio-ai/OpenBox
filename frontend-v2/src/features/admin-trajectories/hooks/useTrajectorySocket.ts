import { useEffect } from "react"
import { useTrajectoryAccess } from "../stores/access"
import { sendTrajectoryMessage, trajectorySocket } from "../api/socket"
import type { TrajectorySync } from "../api/sync"

let users = 0

/**
 * Subscribes to watermark hints for the target and re-subscribes after every
 * reconnect. A hint for another owner or session is ignored; a hint never
 * carries content, it only makes the engine read committed events sooner.
 * Refusals (`__denied`) are handled by the access binding, which purges.
 */
export function useTrajectorySocket(
  sessionId: string,
  ownerId: string | null,
  sync: TrajectorySync | null,
): void {
  const allowed = useTrajectoryAccess((s) => s.denied === null)

  useEffect(() => {
    if (!sync || !ownerId || !allowed) return
    users += 1
    const subscribe = () =>
      sendTrajectoryMessage({
        type: "subscribe",
        session_id: sessionId,
        after_seq: sync.getSnapshot().loadedSeq,
      })
    const forTarget = (data: { session_id: string; user_id: string; owner_user_id?: string }) =>
      data.session_id === sessionId && (data.owner_user_id ?? data.user_id) === ownerId
    const offs = [
      trajectorySocket.on("__connected", subscribe),
      trajectorySocket.on("trajectory.available", (data) => {
        if (forTarget(data)) sync.noteCommitted(data.committed_seq)
      }),
      trajectorySocket.on("subscribed", (data) => {
        if (forTarget(data)) sync.noteCommitted(data.committed_seq)
      }),
    ]
    void trajectorySocket.connect()
    subscribe()
    return () => {
      for (const off of offs) off()
      sendTrajectoryMessage({ type: "unsubscribe", session_id: sessionId })
      users -= 1
      if (users === 0) trajectorySocket.disconnect()
    }
  }, [allowed, ownerId, sessionId, sync])
}
