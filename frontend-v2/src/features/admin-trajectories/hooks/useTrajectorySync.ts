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
 * commits a watermark hint may have missed — every 30 s while the socket is
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
    let polling = false
    let cancelled = false
    // The next read is due an interval after the previous one ended. The socket
    // and the tab decide that interval, so a change of either moves the due
    // time — measured from the last read, never from the change itself: a
    // socket that keeps opening and dropping cannot postpone the poll forever.
    let last = Date.now()
    const arm = () => {
      if (cancelled || polling) return
      if (timer !== null) window.clearTimeout(timer)
      const delay = eventPollDelay(trajectorySocket.connected, document.visibilityState === "hidden")
      timer = window.setTimeout(poll, Math.max(0, last + delay - Date.now()))
    }
    const poll = () => {
      if (timer !== null) window.clearTimeout(timer)
      timer = null
      polling = true
      void sync.poll().finally(() => {
        polling = false
        last = Date.now()
        arm()
      })
    }
    const onVisibility = () => {
      if (document.visibilityState !== "visible") arm()
      // A read already on its way is followed by another one (the engine coalesces them).
      else if (polling) void sync.poll()
      else poll()
    }
    // The socket opening or dropping changes how soon a missed commit must be noticed.
    const offs = [trajectorySocket.on("__connected", arm), trajectorySocket.on("__disconnected", arm)]
    arm()
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
