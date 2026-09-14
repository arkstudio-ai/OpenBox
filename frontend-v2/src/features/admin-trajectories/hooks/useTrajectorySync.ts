import { useEffect, useSyncExternalStore } from "react"
import { useQueryClient } from "@tanstack/react-query"
import { forgetTarget, purgeTrajectoryAccess } from "../api/access"
import { acquireSync, peekSync, releaseSync, subscribeRegistry } from "../api/registry"
import type { SyncSnapshot, TrajectorySync } from "../api/sync"
import { useAccessScope } from "../api/queries"

export const LIVE_POLL_MS = 1_000
export const HIDDEN_POLL_MS = 5_000

const noopSubscribe = () => () => undefined
const noSnapshot = () => null

export interface TrajectorySyncState {
  sync: TrajectorySync | null
  snapshot: SyncSnapshot | null
}

/**
 * The live stream for one target session: acquires its engine, polls the head
 * every second while visible (less often when hidden, and immediately on
 * return), and releases everything when the target changes, the page closes
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
      const delay = document.visibilityState === "hidden" ? HIDDEN_POLL_MS : LIVE_POLL_MS
      timer = window.setTimeout(() => {
        void sync.poll().finally(schedule)
      }, delay)
    }
    const onVisibility = () => {
      if (document.visibilityState === "visible") void sync.poll()
      schedule()
    }
    schedule()
    document.addEventListener("visibilitychange", onVisibility)
    return () => {
      cancelled = true
      if (timer !== null) window.clearTimeout(timer)
      document.removeEventListener("visibilitychange", onVisibility)
    }
  }, [sync])

  return { sync, snapshot }
}
