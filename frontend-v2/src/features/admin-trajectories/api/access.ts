// Everything a viewer may have seen is dropped the moment they may no longer
// see it: on 401/403 from any trajectory request or socket, on sign-out, on a
// role change and on switching accounts. In-flight requests are aborted and
// the epoch moves first-class into every query key, so a late answer can
// neither repopulate the cache nor reach a mounted observer.
import type { QueryClient } from "@tanstack/react-query"
import { useAuthStore } from "@/shared/api/auth-store"
import { ApiError } from "@/shared/api/http"
import { currentAccessEpoch, useTrajectoryAccess, type DenialReason } from "../stores/access"
import { useTrajectoryView } from "../stores/view"
import { trajectoryKeys } from "./keys"
import { parseTargetKey, purgeSyncs } from "./registry"
import { trajectorySocket } from "./socket"

const ROOT = trajectoryKeys.root[0]
const inFlight = new Set<AbortController>()

export class StaleAccessError extends Error {
  constructor() {
    super("Trajectory access changed while the request was in flight")
    this.name = "StaleAccessError"
  }
}

export function isAccessFailure(error: unknown): boolean {
  return error instanceof ApiError && (error.status === 401 || error.status === 403)
}

export function isTrajectoryKey(key: unknown): boolean {
  return Array.isArray(key) && key[0] === ROOT
}

/** Retry policy for trajectory queries: never retry a refusal. */
export function retryUnlessDenied(failureCount: number, error: unknown): boolean {
  return !isAccessFailure(error) && failureCount < 1
}

export interface TrackedRequest {
  signal: AbortSignal
  /** Throws StaleAccessError if a purge or an identity change happened since the request began. */
  check: () => void
  /** Cancel this request alone (its owner unmounted or changed target); `check()` then throws. */
  abort: () => void
  release: () => void
}

function mayRead(): boolean {
  const { user } = useAuthStore.getState()
  return useTrajectoryAccess.getState().denied === null && user?.role === "admin"
}

/**
 * An abortable request tied to the current viewer and epoch. Purges abort it.
 * Call `check()` before dispatching (a still-mounted button must not send a
 * request once access is latched) and again before using the answer.
 */
export function trackRequest(): TrackedRequest {
  const controller = new AbortController()
  const epoch = currentAccessEpoch()
  const viewer = useAuthStore.getState().user?.id ?? null
  inFlight.add(controller)
  return {
    signal: controller.signal,
    check: () => {
      const changed = currentAccessEpoch() !== epoch || (useAuthStore.getState().user?.id ?? null) !== viewer
      if (controller.signal.aborted || changed || !mayRead()) throw new StaleAccessError()
    },
    abort: () => controller.abort(),
    release: () => {
      inFlight.delete(controller)
    },
  }
}

function clearEverything(client: QueryClient | null): void {
  for (const controller of inFlight) controller.abort()
  inFlight.clear()
  if (client) {
    void client.cancelQueries({ queryKey: trajectoryKeys.root })
    client.removeQueries({ queryKey: trajectoryKeys.root })
    const mutations = client.getMutationCache()
    for (const mutation of mutations.findAll({
      predicate: (item) => isTrajectoryKey(item.options.mutationKey),
    })) {
      mutations.remove(mutation)
    }
  }
  purgeSyncs()
  trajectorySocket.disconnect()
  useTrajectoryView.getState().reset()
}

/** Refusal or loss of rights: clear, and stay locked until the identity changes. */
export function purgeTrajectoryAccess(
  client: QueryClient | null,
  reason: DenialReason = "forbidden",
  status: number | null = null,
): void {
  const alreadyDenied = useTrajectoryAccess.getState().denied !== null
  clearEverything(client)
  if (!alreadyDenied) useTrajectoryAccess.getState().revoke(reason, status)
}

/** A different admin (or the same one signing in again): clear and unlock under a new epoch. */
export function resetTrajectoryAccess(client: QueryClient | null): void {
  clearEverything(client)
  useTrajectoryAccess.getState().rotate()
}

/**
 * The target session no longer exists for this viewer (deleted, 404/410 after
 * it was loaded): drop everything cached about it — header, pages, details,
 * search, payloads, exports — and the view state pointing into it.
 */
export function forgetTarget(client: QueryClient, sessionId: string): void {
  const matches = (key: readonly unknown[]) =>
    isTrajectoryKey(key) && key[2] === "target" && key[3] === sessionId
  void client.cancelQueries({ predicate: (query) => matches(query.queryKey) })
  client.removeQueries({ predicate: (query) => matches(query.queryKey) })
  const view = useTrajectoryView.getState()
  if (parseTargetKey(view.targetKey)?.sessionId === sessionId) view.reset()
}

/* ------------------------------ app binding ------------------------------ */

interface Identity {
  id: string
  admin: boolean
}

type AuthSnapshot = ReturnType<typeof useAuthStore.getState>

/** The signed-in identity, null when signed out, undefined while it is still settling. */
export function settledIdentity(state: AuthSnapshot): Identity | null | undefined {
  if (state.isLoading) return undefined
  // A token refresh can leave an authenticated session without a user for a tick.
  if (state.isAuthenticated && state.user === null) return undefined
  if (!state.isAuthenticated || !state.user) return null
  return { id: state.user.id, admin: state.user.role === "admin" }
}

function reasonFor(status: number | null): DenialReason {
  return status === 401 || status === 4401 ? "unauthenticated" : "forbidden"
}

let binding: { client: QueryClient; offs: Array<() => void> } | null = null

/**
 * Wire the purge triggers for this tab. Deliberately not undone on unmount:
 * once trajectory data has been loaded, a sign-out on any page must still
 * clear it.
 */
export function bindTrajectoryAccess(client: QueryClient): void {
  if (binding?.client === client) return
  unbindTrajectoryAccess()
  let last: Identity | null = settledIdentity(useAuthStore.getState()) ?? null
  const offAuth = useAuthStore.subscribe((state) => {
    const identity = settledIdentity(state)
    // Only a settled identity replaces the last one; a transient blank must not
    // erase the fact that an admin was signed in.
    if (identity === undefined) return
    if (identity?.id === last?.id && identity?.admin === last?.admin) return
    if (last?.admin) {
      if (identity === null) purgeTrajectoryAccess(client, "signed_out")
      else if (identity.id !== last.id && identity.admin) resetTrajectoryAccess(client)
      else if (identity.id !== last.id) purgeTrajectoryAccess(client, "identity_changed")
      else purgeTrajectoryAccess(client, "role_changed")
    } else if (identity?.admin) {
      resetTrajectoryAccess(client)
    }
    last = identity
  })
  const offQueries = client.getQueryCache().subscribe((event) => {
    if (event.type !== "updated" || event.action.type !== "error") return
    if (!isTrajectoryKey(event.query.queryKey) || !isAccessFailure(event.action.error)) return
    const status = (event.action.error as ApiError).status
    purgeTrajectoryAccess(client, reasonFor(status), status)
  })
  const offMutations = client.getMutationCache().subscribe((event) => {
    if (event.type !== "updated" || event.action.type !== "error") return
    if (!isTrajectoryKey(event.mutation.options.mutationKey) || !isAccessFailure(event.action.error)) return
    const status = (event.action.error as ApiError).status
    purgeTrajectoryAccess(client, reasonFor(status), status)
  })
  const offSocket = trajectorySocket.on("__denied", ({ status }) =>
    purgeTrajectoryAccess(client, reasonFor(status), status),
  )
  binding = { client, offs: [offAuth, offQueries, offMutations, offSocket] }
}

export function unbindTrajectoryAccess(): void {
  for (const off of binding?.offs ?? []) off()
  binding = null
}
