import { useEffect, useMemo, useRef, useState } from "react"
import { useQueryClient } from "@tanstack/react-query"
import { ApiError } from "@/shared/api/http"
import { isAccessFailure, purgeTrajectoryAccess, trackRequest, type TrackedRequest } from "../api/access"
import { trajectoryApi } from "../api/endpoints"
import { LIST_AUDIENCE_REVALIDATE_MS, LIST_AUDIENCE_TIMEOUT_MS } from "../constants/polling"
import type { SessionPage } from "../types/protocol"

const unavailable = () => new ApiError(503, "trajectory_auth_unavailable", "Trajectory authorization unavailable")

/** Frozen order is useful, but never a permanent grant to display old titles. */
export function useSessionPageAudience(
  page: SessionPage | undefined,
  { viewer, enabled, updatedAt, onRevoked }: {
    viewer: string; enabled: boolean; updatedAt: number; onRevoked: () => Promise<unknown>
  },
) {
  const client = useQueryClient()
  // Even A -> B -> cached A needs new proof; verdicts never live in the query cache.
  const context = useMemo(() => ({ page, viewer, enabled, updatedAt }), [page, viewer, enabled, updatedAt])
  const [proof, setProof] = useState<{
    context: typeof context; allowed: boolean; checking: boolean; error: unknown
  } | null>(null)
  const refresh = useRef<() => void>(() => undefined)

  useEffect(() => {
    if (!enabled || !page?.items.length) return
    let disposed = false
    let active: TrackedRequest | null = null
    let timer: number | undefined
    let deadline: number | undefined
    const arm = () => { timer = window.setTimeout(() => check(false), LIST_AUDIENCE_REVALIDATE_MS) }
    const check = (hide: boolean) => {
      window.clearTimeout(timer)
      window.clearTimeout(deadline)
      active?.abort()
      active?.release()
      const request = trackRequest()
      active = request
      setProof((previous) => ({
        context, allowed: !hide && previous?.context === context && previous.allowed,
        checking: true, error: null,
      }))
      deadline = window.setTimeout(() => {
        if (disposed || active !== request) return
        request.abort()
        request.release()
        active = null
        setProof({ context, allowed: false, checking: false, error: unavailable() })
        arm()
      }, LIST_AUDIENCE_TIMEOUT_MS)
      void (async () => {
        try {
          request.check()
          const allowed = await trajectoryApi.sessionAudience(page.items, request.signal)
          if (disposed || active !== request) return
          request.check()
          const readable = page.items.every((row) => allowed.includes(row.session_id))
          setProof({ context, allowed: readable, checking: false, error: null })
          // Rebuild paging metadata too. Filtering only items would retain a revoked tail/cursor.
          if (!readable) await onRevoked()
        } catch (error) {
          if (disposed || active !== request) return
          setProof({ context, allowed: false, checking: false, error: unavailable() })
          // A late refusal for an old identity must not lock out the new viewer.
          try { request.check() } catch { return }
          if (isAccessFailure(error)) {
            const status = (error as ApiError).status
            purgeTrajectoryAccess(client, status === 401 ? "unauthenticated" : "forbidden", status)
          }
        } finally {
          request.release()
          if (!disposed && active === request) {
            window.clearTimeout(deadline)
            active = null
            arm()
          }
        }
      })()
    }
    const foreground = () => {
      if (document.visibilityState !== "hidden") check(true)
    }
    refresh.current = () => check(true)
    check(true)
    window.addEventListener("focus", foreground)
    document.addEventListener("visibilitychange", foreground)
    return () => {
      disposed = true
      refresh.current = () => undefined
      window.clearTimeout(timer)
      window.clearTimeout(deadline)
      active?.abort()
      active?.release()
      window.removeEventListener("focus", foreground)
      document.removeEventListener("visibilitychange", foreground)
    }
  }, [client, context, enabled, onRevoked, page])

  const current = proof?.context === context ? proof : null
  const ready = enabled && !!page && (!page.items.length || !!current?.allowed)
  return {
    data: ready ? page : undefined,
    checking: enabled && !!page?.items.length && (!current || current.checking),
    error: current?.error ?? null,
    refresh: () => refresh.current(),
  }
}
