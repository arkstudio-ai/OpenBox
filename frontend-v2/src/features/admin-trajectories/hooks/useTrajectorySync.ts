import { useEffect, useSyncExternalStore } from "react"
import { useQueryClient } from "@tanstack/react-query"
import { forgetTarget, purgeTrajectoryAccess } from "../api/access"
import { acquireSync, peekSync, releaseSync, subscribeRegistry } from "../api/registry"
import { trajectorySocket } from "../api/socket"
import type { SyncSnapshot, TrajectorySync } from "../api/sync"
import { useAccessScope } from "../api/queries"
import { eventPollDelay } from "../constants/polling"

const noopSubscribe = () => () => undefined
const noSnapshot = () => null

export interface TrajectorySyncState {
  sync: TrajectorySync | null
  snapshot: SyncSnapshot | null
}

/**
 * The live stream for one target session: acquires its engine, polls for
 * commits a watermark hint may have missed — every 10 s while the socket is
 * open, every 2 s while it is not, less often when hidden and immediately on
 * return — and releases everything when the target changes, the page closes
 * or access is refused.
 */
export function useTrajectorySync(sessionId: string, enabled: boolean): TrajectorySyncState {
  const { viewerId, allowed } = useAccessScope()
  const client = useQueryClient()
  const active = enabled && allowed

  useEffect(() => {
    if (!active) return
    acquireSync(viewerId, sessionId, {
      onDenied: (error) =>
        purgeTrajectoryAccess(
          client,
          error.status === 401 ? "unauthenticated" : "forbidden",
          error.status ?? null,
        ),
      onGone: () => forgetTarget(client, sessionId),
    })
    return () => releaseSync(viewerId, sessionId)
  }, [active, client, sessionId, viewerId])

  const sync = useSyncExternalStore(subscribeRegistry, () => (active ? peekSync(viewerId, sessionId) : null))
  const snapshot = useSyncExternalStore(
    sync ? sync.subscribe : noopSubscribe,
    sync ? sync.getSnapshot : noSnapshot,
  )

  useEffect(() => {
    if (!sync) return
    let timer: number | null = null
    let cancelled = false
    const schedule = () => {
      if (cancelled) return
      if (timer !== null) window.clearTimeout(timer)
      const delay = eventPollDelay(trajectorySocket.connected, document.visibilityState === "hidden")
      timer = window.setTimeout(() => {
        timer = null
        void sync.poll().finally(schedule)
      }, delay)
    }
    const onVisibility = () => {
      if (document.visibilityState === "visible") void sync.poll()
      schedule()
    }
    // The socket opening or dropping changes how soon a missed commit must be noticed.
    const offs = [
      trajectorySocket.on("__connected", schedule),
      trajectorySocket.on("__disconnected", schedule),
    ]
    schedule()
    document.addEventListener("visibilitychange", onVisibility)
    return () => {
      cancelled = true
      if (timer !== null) window.clearTimeout(timer)
      for (const off of offs) off()
      document.removeEventListener("visibilitychange", onVisibility)
    }
  }, [sync])

  return { sync, snapshot }
}
