import { useEffect, useRef } from "react"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import {
  isAccessFailure,
  purgeTrajectoryAccess,
  StaleAccessError,
  trackRequest,
  type TrackedRequest,
} from "../api/access"
import { trajectoryKeys } from "../api/keys"
import { loadPayload, useAccessScope } from "../api/queries"
import type { Seq } from "../types/protocol"
import { saveBlob } from "../utils/download"

export interface PayloadDownloadVariables {
  /** Captured name to offer; any directory part is dropped. Defaults to the payload id. */
  filename?: string | null
}

/** What a click achieved. Never carries the bytes: the Blob goes to the browser and nowhere else. */
export type PayloadDownloadOutcome =
  { saved: true } | { saved: false; availability: "pending" | "deleted" | "corrupt" }

function fileNameFor(variables: PayloadDownloadVariables | void, payloadId: string): string {
  const captured = variables?.filename?.split(/[\\/]/).pop()?.trim()
  return captured || payloadId
}

function isAbort(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError"
}

/**
 * Explicit download of protected content at watermark H. Every call performs
 * a fresh GET of the admin payload endpoint — a Blob already on screen is never
 * reused — so the server re-applies current rights and deletion on each click.
 *
 * - Refused (401/403): trajectory state is purged and the call rejects.
 * - Not produced at H (404), deleted (410) or corrupt (409): nothing is saved,
 *   and the shown payload query is replaced by the body-less state so the
 *   displayed media and button disappear.
 * - Access, viewer or epoch changed, the owner unmounted, or the target/H
 *   changed while the bytes were travelling: nothing is saved and the call
 *   rejects with `StaleAccessError`.
 */
export function useDownloadPayload(sessionId: string, throughSeq: Seq, payloadId: string) {
  const scope = useAccessScope()
  const client = useQueryClient()
  const inFlight = useRef(new Set<TrackedRequest>())

  // A late answer belongs to the component, target and position that asked.
  useEffect(() => {
    const requests = inFlight.current
    return () => {
      for (const request of requests) request.abort()
      requests.clear()
    }
  }, [sessionId, throughSeq, payloadId])

  return useMutation<PayloadDownloadOutcome, Error, PayloadDownloadVariables | void>({
    mutationKey: [
      ...trajectoryKeys.target(scope.viewer, sessionId),
      "payload-download",
      throughSeq,
      payloadId,
    ],
    mutationFn: async (variables) => {
      const request = trackRequest()
      inFlight.current.add(request)
      try {
        request.check()
        const content = await loadPayload(sessionId, payloadId, throughSeq, request.signal)
        request.check()
        if (content.availability !== "available") {
          client.setQueryData(trajectoryKeys.payload(scope.viewer, sessionId, throughSeq, payloadId), content)
          return { saved: false, availability: content.availability }
        }
        saveBlob(content.blob, fileNameFor(variables, payloadId))
        return { saved: true }
      } catch (error) {
        if (isAccessFailure(error)) {
          purgeTrajectoryAccess(
            client,
            (error as { status: number }).status === 401 ? "unauthenticated" : "forbidden",
            (error as { status: number }).status,
          )
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
}
