// One sync engine per (viewer, target session). The engine holds the only copy
// of a target's streamed events, so dropping it is how sensitive local state is
// erased on sign-out, demotion or a target switch. Components read the registry
// through `useSyncExternalStore`, so acquiring in an effect needs no setState.
import { trajectoryApi } from "./endpoints"
import { TrajectorySync, type SyncOptions } from "./sync"

interface Entry {
  sync: TrajectorySync
  users: number
}

const engines = new Map<string, Entry>()
const listeners = new Set<() => void>()

function notify(): void {
  for (const listener of listeners) listener()
}

/**
 * The one target identity format shared by the engine registry and the view
 * store (`bindTarget`). Viewer ids never contain a space; session ids may
 * contain anything, so parse from the first space.
 */
export function targetKey(viewerId: string, sessionId: string): string {
  return `${viewerId} ${sessionId}`
}

export function parseTargetKey(
  key: string | null | undefined,
): { viewerId: string; sessionId: string } | null {
  if (!key) return null
  const space = key.indexOf(" ")
  return space > 0 ? { viewerId: key.slice(0, space), sessionId: key.slice(space + 1) } : null
}

export function subscribeRegistry(listener: () => void): () => void {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}

export function peekSync(viewerId: string, sessionId: string): TrajectorySync | null {
  return engines.get(targetKey(viewerId, sessionId))?.sync ?? null
}

export function acquireSync(viewerId: string, sessionId: string, options: SyncOptions = {}): TrajectorySync {
  const key = targetKey(viewerId, sessionId)
  const existing = engines.get(key)
  if (existing) {
    existing.users += 1
    return existing.sync
  }
  const sync = new TrajectorySync(
    {
      events: (params, signal) => trajectoryApi.events(sessionId, params, signal),
      checkpoint: (atSeq, signal) => trajectoryApi.checkpoint(sessionId, atSeq, signal),
    },
    options,
  )
  engines.set(key, { sync, users: 1 })
  void sync.open()
  notify()
  return sync
}

export function releaseSync(viewerId: string, sessionId: string): void {
  const key = targetKey(viewerId, sessionId)
  const entry = engines.get(key)
  if (!entry) return
  entry.users -= 1
  if (entry.users > 0) return
  entry.sync.stop()
  engines.delete(key)
  notify()
}

/** Stop every engine and forget its events. */
export function purgeSyncs(): void {
  for (const entry of engines.values()) entry.sync.stop()
  engines.clear()
  notify()
}

export function activeSyncCount(): number {
  return engines.size
}
