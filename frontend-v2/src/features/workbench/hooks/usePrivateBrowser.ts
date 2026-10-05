import { useCallback, useEffect, useRef, useState } from "react"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import { privateBrowserApi, sameBrowserFence, type BrowserAction, type BrowserCommand, type BrowserControlReceipt,
  type BrowserFrame, type BrowserGrant, type BrowserOperation, type BrowserSnapshot } from "../api/private-browser"

function active(resource: BrowserSnapshot, grant: BrowserGrant) {
  return resource.remote_available && resource.status === "active" && resource.admission === "open"
    && sameBrowserFence(resource.fence, grant.fence) && Date.parse(grant.expires_at) > Date.now()
}

function takeoverGrant(receipt: BrowserControlReceipt, userId: string, resourceId: string): BrowserGrant {
  const { fence, human_token, expires_at } = receipt
  if (!fence || fence.owner_kind !== "human" || fence.owner_id !== userId
      || fence.resource_id !== resourceId || !human_token
      || !expires_at || Date.parse(expires_at) <= Date.now()) throw new Error("Unavailable grant")
  return { fence, human_token, expires_at }
}

/** One mounted actor/workspace owns the ephemeral grant and its renewal. */
export function usePrivateBrowser(userId: string, workspaceId: string) {
  const [resource, setResource] = useState<BrowserSnapshot | null>(null)
  const [grant, setGrant] = useState<BrowserGrant | null>(null)
  const [frame, setFrame] = useState<BrowserFrame | null>(null)
  const [pending, setPending] = useState<BrowserCommand | null>(null)
  const [busy, setBusy] = useState(false)
  const [loaded, setLoaded] = useState(false)
  const [operationError, setOperationError] = useState(false)
  const [readError, setReadError] = useState(false)
  const [givenBack, setGivenBack] = useState<{ resumed: number; changed: number } | null>(null)
  const runtime = useRef({ version: 0, readVersion: 0, operationVersion: 0, busy: 0, controlling: false, polling: false, renewing: false,
    grant: null as BrowserGrant | null, resource: null as BrowserSnapshot | null,
    abort: new AbortController() })

  const current = useCallback((version: number) => version === runtime.current.version
    && !runtime.current.abort.signal.aborted
    && useAuthStore.getState().user?.id === userId
    && useWorkspaceStore.getState().currentId === workspaceId, [userId, workspaceId])

  const clearGrant = useCallback(() => {
    runtime.current.grant = null
    setGrant(null)
    setFrame(null)
  }, [])

  const snapshot = useCallback((next: BrowserSnapshot | null) => {
    runtime.current.resource = next
    setResource(next)
    setLoaded(true)
    if (next?.pending_control) {
      const { action, expected_epoch, idempotency_key } = next.pending_control
      setPending({ resourceId: next.resource_id, action, expected_epoch, idempotency_key })
    }
    if (runtime.current.grant && (!next || !active(next, runtime.current.grant))) clearGrant()
  }, [clearGrant])

  const refresh = useCallback(async (force = false, automatic = false) => {
    const state = runtime.current
    const version = state.version
    if (!current(version) || (state.polling && !force)) return
    const readVersion = ++state.readVersion
    const operationVersion = state.operationVersion
    state.polling = true
    try {
      const result = await privateBrowserApi(workspaceId, state.abort.signal).current()
      if (current(version) && state.readVersion === readVersion) {
        snapshot(result.resource)
        setReadError(false)
        // A background status read does not confirm a failed user operation.
        if (!automatic && state.operationVersion === operationVersion) setOperationError(false)
      }
    } catch {
      if (current(version) && state.readVersion === readVersion) { snapshot(null); clearGrant(); setReadError(true) }
    } finally {
      if (state.readVersion === readVersion) state.polling = false
    }
  }, [clearGrant, current, snapshot, workspaceId])

  const capture = useCallback(async (saved: BrowserGrant, version: number) => {
    const receipt = await privateBrowserApi(workspaceId, runtime.current.abort.signal).operation(saved, "capture", {})
    if (!current(version) || runtime.current.grant !== saved) return
    const next = receipt.result
    if (receipt.state !== "completed" || !sameBrowserFence(receipt.fence, saved.fence)
        || !next.png_base64 || !next.observation?.eligible
        || !sameBrowserFence(next.observation.fence, saved.fence)
        || next.observation.width !== 1024 || next.observation.height !== 768) throw new Error("Unavailable observation")
    setFrame(next as BrowserFrame)
  }, [current, workspaceId])

  const run = useCallback(async (operation: (version: number) => Promise<void>, controlOperation = false) => {
    const state = runtime.current
    const version = state.version
    if (!current(version) || (controlOperation ? state.controlling : state.busy > 0)) return
    state.busy += 1
    state.operationVersion += 1
    if (controlOperation) state.controlling = true
    setBusy(true)
    setOperationError(false)
    setReadError(false)
    try { await operation(version) }
    catch { if (current(version)) { clearGrant(); setOperationError(true) } }
    finally {
      if (current(version)) {
        state.busy -= 1
        if (controlOperation) state.controlling = false
        setBusy(state.busy > 0)
      }
    }
  }, [clearGrant, current])

  const ensure = useCallback(() => run(async (version) => {
    const result = await privateBrowserApi(workspaceId, runtime.current.abort.signal).ensure()
    if (current(version)) snapshot(result)
  }), [current, run, snapshot, workspaceId])

  const control = useCallback((action: BrowserAction, original?: BrowserCommand) => run(async (version) => {
    const row = runtime.current.resource
    if (!original && (!row || (action === "takeover" && !row.can_takeover)
        || (action === "giveback" && !row.can_giveback))) return
    const command = original ?? { resourceId: row!.resource_id, action,
      expected_epoch: row!.fence.epoch, idempotency_key: crypto.randomUUID() }
    // Retain this exact command through a timeout/drain; never rebase its epoch.
    setPending(command)
    runtime.current.readVersion += 1
    // The retired read can no longer clear polling in its finally block.
    // Release its slot even if this control fails before a forced refresh.
    runtime.current.polling = false
    clearGrant()
    setGivenBack(null)
    const receipt = await privateBrowserApi(workspaceId, runtime.current.abort.signal).control(command)
    if (!current(version)) return
    if (receipt.state === "draining") { await refresh(true); return }
    if (receipt.state !== "applied") throw new Error("Unconfirmed handover")
    setPending(null)
    // A recovered, expired receipt records history without issuing a token.
    // Read the held epoch now so the user can explicitly give it back.
    if (receipt.human_grant_expired === true) { await refresh(true); return }
    if (action === "takeover") {
      const saved = takeoverGrant(receipt, userId, command.resourceId)
      runtime.current.grant = saved
      setGrant(saved)
      await refresh(true)
      if (runtime.current.grant === saved) await capture(saved, version)
    } else {
      setGivenBack(action === "giveback" ? { resumed: receipt.resume_requested_task_ids?.length ?? 0,
        changed: receipt.task_control_changed_ids?.length ?? 0 } : null)
      await refresh(true)
    }
  }, true), [capture, clearGrant, current, refresh, run, userId, workspaceId])

  const operate = useCallback((kind: BrowserOperation, args: Record<string, unknown> = {}) => run(async (version) => {
    const saved = runtime.current.grant
    const row = runtime.current.resource
    if (!saved || !row || !active(row, saved)) { clearGrant(); return }
    if (kind === "capture") { await capture(saved, version); return }
    setFrame(null)
    const receipt = await privateBrowserApi(workspaceId, runtime.current.abort.signal).operation(saved, kind, args)
    if (!current(version) || runtime.current.grant !== saved) return
    if (receipt.state !== "completed" || !sameBrowserFence(receipt.fence, saved.fence)
        || receipt.result.navigation_error) throw new Error("Browser operation unavailable")
    await capture(saved, version)
  }), [capture, clearGrant, current, run, workspaceId])

  useEffect(() => {
    const state = runtime.current
    state.version += 1
    state.abort = new AbortController()
    state.polling = false
    void refresh(false, true)
    const poll = window.setInterval(() => { void refresh(false, true) }, 5_000)
    const heartbeat = window.setInterval(async () => {
      const saved = state.grant
      const version = state.version
      if (!saved || !current(version) || state.renewing) return
      state.renewing = true
      try {
        const receipt = await privateBrowserApi(workspaceId, state.abort.signal).heartbeat(saved)
        if (!current(version) || state.grant !== saved) return
        if (!sameBrowserFence(receipt.fence, saved.fence) || Date.parse(receipt.expires_at) <= Date.now())
          throw new Error("Expired browser grant")
        // Keep the same grant identity so an in-flight capture remains bound.
        saved.expires_at = receipt.expires_at
        setGrant({ ...saved })
      } catch {
        if (current(version)) { state.operationVersion += 1; clearGrant(); setOperationError(true) }
      }
      finally { state.renewing = false }
    }, 30_000)
    return () => {
      state.version += 1
      state.abort.abort()
      state.grant = null
      window.clearInterval(poll)
      window.clearInterval(heartbeat)
    }
  }, [clearGrant, current, refresh, workspaceId])

  return { resource, frame, pending, busy, loaded, error: operationError || readError, givenBack,
    controlled: !!(grant && resource && active(resource, grant)), refresh, ensure, control, operate }
}
