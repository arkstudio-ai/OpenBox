import { useEffect, useRef } from "react"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { ApiError } from "@/shared/api/http"
import {
  isAccessFailure,
  purgeTrajectoryAccess,
  StaleAccessError,
  trackRequest,
  type TrackedRequest,
} from "../api/access"
import { trajectoryApi } from "../api/endpoints"
import { trajectoryKeys } from "../api/keys"
import { useAccessScope } from "../api/queries"
import type { Seq } from "../types/protocol"

export interface PayloadLinkVariables {
  /** Ask for a download link (Content-Disposition attachment) with this file name. */
  download?: boolean
  filename?: string | null
}

/** What a click achieved; the signed URL goes to the browser window and nowhere else. */
export type PayloadLinkOutcome =
  | { opened: true; expiresIn: number }
  | { opened: false; reason: "pending" | "deleted" | "corrupt" | "not_linkable" }

const REASONS: Record<number, PayloadLinkOutcome & { opened: false } extends { reason: infer R } ? R : never> = {
  404: "pending",
  410: "deleted",
}

function isAbort(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError"
}

/**
 * Opens the person's actual output file (a video, image or other asset the
 * trajectory recorded by reference) in a new window through a short-lived
 * signed link from the admin trajectory API. Every click asks the server
 * afresh: it audits the link, re-applies current rights and deletion, and
 * signs for minutes. A retained trajectory copy (no source object) answers
 * `not_linkable`; the button is only offered for artifacts with a source asset.
 *
 * The window is opened synchronously in the click (popup blockers) and pointed
 * at the URL once it arrives; on any refusal it is closed again.
 */
export function useOpenPayloadLink(sessionId: string, throughSeq: Seq, payloadId: string) {
  const scope = useAccessScope()
  const client = useQueryClient()
  const inFlight = useRef(new Set<TrackedRequest>())

  useEffect(() => {
    const requests = inFlight.current
    return () => {
      for (const request of requests) request.abort()
      requests.clear()
    }
  }, [sessionId, throughSeq, payloadId])

  const mutation = useMutation<PayloadLinkOutcome, Error, PayloadLinkVariables & { target?: Window | null }>({
    mutationKey: [...trajectoryKeys.target(scope.viewer, sessionId), "payload-link", throughSeq, payloadId],
    mutationFn: async (variables) => {
      const request = trackRequest()
      inFlight.current.add(request)
      const target = variables.target ?? null
      try {
        request.check()
        const link = await trajectoryApi.payloadLink(sessionId, payloadId, throughSeq, {
          download: variables.download,
          name: variables.filename ?? null,
          signal: request.signal,
        })
        request.check()
        if (target && !target.closed) target.location.replace(link.url)
        else window.open(link.url, "_blank", "noopener,noreferrer")
        return { opened: true, expiresIn: link.expires_in }
      } catch (error) {
        target?.close()
        if (error instanceof ApiError) {
          if (isAccessFailure(error)) {
            purgeTrajectoryAccess(client, error.status === 401 ? "unauthenticated" : "forbidden", error.status)
          }
          if (error.status === 409 && error.code === "trajectory_payload_not_linkable") {
            return { opened: false, reason: "not_linkable" }
          }
          if (error.status === 409) return { opened: false, reason: "corrupt" }
          const reason = REASONS[error.status]
          if (reason) return { opened: false, reason }
        }
        if (isAbort(error)) throw new StaleAccessError()
        throw error
      } finally {
        request.release()
        inFlight.current.delete(request)
      }
    },
    retry: false,
  })

  /** Call from the click handler: opens the window first, then fills it. */
  const open = (variables: PayloadLinkVariables = {}) => {
    const target = typeof window.open === "function" ? window.open("", "_blank", "noopener,noreferrer") : null
    return mutation.mutateAsync({ ...variables, target })
  }

  return { ...mutation, open }
}
