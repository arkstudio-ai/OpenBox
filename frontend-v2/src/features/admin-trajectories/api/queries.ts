// Query hooks. Keys come from ./keys: viewer (with access epoch) + target +
// watermark. Pages read at a fixed watermark are immutable and never go stale;
// the header and list probe follow the moving head; protected content is
// revalidated because a deletion overrides every historical watermark. Every
// hook stops reading once access has been refused (stores/access).
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useAuthStore } from "@/shared/api/auth-store"
import { ApiError } from "@/shared/api/http"
import { currentAccessEpoch, useTrajectoryAccess } from "../stores/access"
import type { ExportJob, Seq } from "../types/protocol"
import { serializeListParams, toApiParams, type ListParams } from "../utils/params"
import { saveBlob } from "../utils/download"
import { retryUnlessDenied, trackRequest } from "./access"
import { trajectoryApi } from "./endpoints"
import { LIVE, trajectoryKeys } from "./keys"

export const SESSION_PAGE_SIZE = 50
export const RECORD_PAGE_SIZE = 100
export const SEARCH_PAGE_SIZE = 50

/** Plain text/JSON payloads above this size are offered as a download only. */
export const INLINE_PAYLOAD_LIMIT = 2 * 1024 * 1024
/** How often shown protected content re-checks that it has not been deleted. */
export const PAYLOAD_REVALIDATE_MS = 15_000

const hidden = () => typeof document !== "undefined" && document.visibilityState === "hidden"
const visibleInterval = (visible: number, whenHidden: number | false) => () =>
  hidden() ? whenHidden : visible

export function useViewerId(): string {
  return useAuthStore((s) => s.user?.id ?? "anon")
}

export interface AccessScope {
  viewerId: string
  /** Viewer plus access epoch: the key segment that makes pre-purge data unreachable. */
  viewer: string
  allowed: boolean
}

export function useAccessScope(): AccessScope {
  const viewerId = useViewerId()
  const epoch = useTrajectoryAccess((s) => s.epoch)
  const denied = useTrajectoryAccess((s) => s.denied)
  return { viewerId, viewer: `${viewerId}#${epoch}`, allowed: denied === null }
}

function sessionListQuery(viewer: string, params: ListParams, limit: number) {
  return {
    queryKey:
      limit === 1
        ? trajectoryKeys.sessionsProbe(viewer, serializeListParams(params))
        : trajectoryKeys.sessions(viewer, serializeListParams(params)),
    queryFn: ({ signal }: { signal: AbortSignal }) =>
      trajectoryApi.listSessions({ ...toApiParams(params), limit }, signal),
  }
}

export function useSessionList(params: ListParams) {
  const scope = useAccessScope()
  return useQuery({
    ...sessionListQuery(scope.viewer, params, SESSION_PAGE_SIZE),
    enabled: scope.allowed,
    // A page is a snapshot: rows must not reorder under the reader. Refresh is explicit.
    staleTime: Infinity,
    retry: retryUnlessDenied,
  })
}

/** The first page for these filters and sort, if already loaded. Subscribes; never fetches. */
export function useCachedFirstPage(params: ListParams) {
  const scope = useAccessScope()
  const first = { ...params, cursor: null, trail: [] }
  return useQuery({
    ...sessionListQuery(scope.viewer, first, SESSION_PAGE_SIZE),
    enabled: false,
    staleTime: Infinity,
  }).data
}

/** Newest row for the current filters and sort, polled so the list can say "updated" without reordering. */
export function useSessionListProbe(params: ListParams, enabled: boolean) {
  const scope = useAccessScope()
  const first = { ...params, cursor: null, trail: [] }
  return useQuery({
    ...sessionListQuery(scope.viewer, first, 1),
    enabled: enabled && scope.allowed,
    refetchInterval: visibleInterval(5_000, 30_000),
    refetchIntervalInBackground: true,
    retry: retryUnlessDenied,
  })
}

/** Live header: identity and current statuses. Its statistics describe the head, not a replay position. */
export function useSessionHeader(sessionId: string) {
  const scope = useAccessScope()
  return useQuery({
    queryKey: trajectoryKeys.header(scope.viewer, sessionId, LIVE),
    queryFn: ({ signal }) => trajectoryApi.header(sessionId, undefined, signal),
    enabled: scope.allowed,
    refetchInterval: visibleInterval(5_000, 30_000),
    refetchIntervalInBackground: true,
    retry: retryUnlessDenied,
  })
}

/** Header rebuilt by the server at a fixed watermark, for views that cannot fold events locally. */
export function useSessionHeaderAt(sessionId: string, throughSeq: Seq | null) {
  const scope = useAccessScope()
  return useQuery({
    queryKey: trajectoryKeys.header(scope.viewer, sessionId, throughSeq ?? ""),
    queryFn: ({ signal }) => trajectoryApi.header(sessionId, throughSeq ?? "0", signal),
    enabled: scope.allowed && throughSeq !== null,
    staleTime: Infinity,
    retry: retryUnlessDenied,
  })
}

export interface RecordPageFilters {
  kind?: string
  status?: string
  agentId?: string
}

/**
 * Server record summaries at a fixed watermark, newest page first, older pages
 * on demand. Callers pin `throughSeq` while paging: a moving head must not
 * rebuild the list under a reader who is scrolled into older pages.
 */
export function useRecordPages(
  sessionId: string,
  throughSeq: Seq | null,
  filters: RecordPageFilters,
  enabled: boolean,
) {
  const scope = useAccessScope()
  return useInfiniteQuery({
    queryKey: trajectoryKeys.recordPages(scope.viewer, sessionId, throughSeq ?? "", JSON.stringify(filters)),
    queryFn: ({ pageParam, signal }) =>
      trajectoryApi.records(
        sessionId,
        { throughSeq: throughSeq ?? "0", before: pageParam, limit: RECORD_PAGE_SIZE, ...filters },
        signal,
      ),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => (last.has_more ? last.next_cursor : undefined),
    enabled: enabled && scope.allowed && throughSeq !== null,
    staleTime: Infinity,
    retry: retryUnlessDenied,
  })
}

export function useRecordDetail(
  sessionId: string,
  throughSeq: Seq | null,
  recordId: string | null,
  enabled: boolean,
) {
  const scope = useAccessScope()
  return useQuery({
    queryKey: trajectoryKeys.record(scope.viewer, sessionId, throughSeq ?? "", recordId ?? ""),
    queryFn: ({ signal }) => trajectoryApi.record(sessionId, recordId ?? "", throughSeq ?? "0", signal),
    enabled: enabled && scope.allowed && !!recordId && throughSeq !== null,
    staleTime: Infinity,
    retry: retryUnlessDenied,
  })
}

/**
 * Full-range server search at a fixed watermark. No placeholder: while a new
 * position or query loads, hits from another watermark (possibly a later one)
 * or another session must not stay on screen.
 */
export function useRecordSearch(sessionId: string, throughSeq: Seq | null, query: string) {
  const scope = useAccessScope()
  const text = query.trim()
  return useInfiniteQuery({
    queryKey: trajectoryKeys.search(scope.viewer, sessionId, throughSeq ?? "", text),
    queryFn: ({ pageParam, signal }) =>
      trajectoryApi.search(
        sessionId,
        { q: text, throughSeq: throughSeq ?? "0", cursor: pageParam, limit: SEARCH_PAGE_SIZE },
        signal,
      ),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => (last.has_more ? last.next_cursor : undefined),
    enabled: scope.allowed && text.length > 0 && throughSeq !== null,
    staleTime: Infinity,
    retry: retryUnlessDenied,
  })
}

/**
 * Protected content at a watermark. The server answers 404 before the content
 * existed at that position, 410 once explicitly deleted and 409 when corrupt;
 * those resolve to a body-less state rather than an error, so a revalidation
 * that discovers a deletion replaces — and releases — the Blob held before.
 */
export type PayloadContent =
  | { availability: "available"; mediaType: string; size: number; blob: Blob; text: string | null }
  | { availability: "pending" | "deleted" | "corrupt"; mediaType: null; size: null; blob: null; text: null }

function isTextual(mediaType: string): boolean {
  return mediaType.startsWith("text/") || mediaType.includes("json") || mediaType.includes("xml")
}

const UNAVAILABLE: Readonly<Record<number, "pending" | "deleted" | "corrupt">> = {
  404: "pending",
  410: "deleted",
  409: "corrupt",
}

export async function loadPayload(
  sessionId: string,
  payloadId: string,
  throughSeq: Seq,
  signal?: AbortSignal,
): Promise<PayloadContent> {
  try {
    const { blob } = await trajectoryApi.payload(sessionId, payloadId, throughSeq, signal)
    const mediaType = blob.type || "application/octet-stream"
    const text = isTextual(mediaType) && blob.size <= INLINE_PAYLOAD_LIMIT ? await blob.text() : null
    return { availability: "available", mediaType, size: blob.size, blob, text }
  } catch (error) {
    const availability = error instanceof ApiError ? UNAVAILABLE[error.status] : undefined
    if (availability) return { availability, mediaType: null, size: null, blob: null, text: null }
    throw error
  }
}

export function usePayload(sessionId: string, throughSeq: Seq | null, payloadId: string | null) {
  const scope = useAccessScope()
  return useQuery({
    queryKey: trajectoryKeys.payload(scope.viewer, sessionId, throughSeq ?? "", payloadId ?? ""),
    queryFn: ({ signal }) => loadPayload(sessionId, payloadId ?? "", throughSeq ?? "0", signal),
    enabled: scope.allowed && !!payloadId && throughSeq !== null,
    // Deletion wins over any watermark, so shown content keeps asking; no Blob
    // outlives the component that displays it.
    staleTime: 0,
    gcTime: 0,
    refetchOnMount: "always",
    refetchOnWindowFocus: true,
    refetchInterval: visibleInterval(PAYLOAD_REVALIDATE_MS, false),
    retry: retryUnlessDenied,
  })
}

interface MutationScope {
  viewer: string
  viewerId: string
  epoch: number
}

function stillCurrent(scope: MutationScope | undefined): scope is MutationScope {
  return (
    !!scope &&
    scope.epoch === currentAccessEpoch() &&
    (useAuthStore.getState().user?.id ?? "anon") === scope.viewerId
  )
}

/** The one persisted admin action. Creates an export job fixed at `throughSeq`. */
export function useCreateExport(sessionId: string) {
  const scope = useAccessScope()
  const client = useQueryClient()
  return useMutation({
    mutationKey: [...trajectoryKeys.target(scope.viewer, sessionId), "export-create"],
    // Identity as of the click. Options re-render after a purge, so callbacks
    // must not read the (new) render scope — they would file an old answer
    // under the new epoch's key.
    onMutate: (): MutationScope => ({
      viewer: scope.viewer,
      viewerId: scope.viewerId,
      epoch: currentAccessEpoch(),
    }),
    mutationFn: async (throughSeq: Seq) => {
      const request = trackRequest()
      try {
        request.check()
        const job = await trajectoryApi.createExport(sessionId, throughSeq, request.signal)
        request.check()
        return job
      } finally {
        request.release()
      }
    },
    onSuccess: (job, _throughSeq, started) => {
      // Re-checked here as well: a purge can land between mutationFn and this callback.
      if (!stillCurrent(started)) return
      client.setQueryData(trajectoryKeys.exportJob(started.viewer, sessionId, job.export_id), job)
      void client.invalidateQueries({
        queryKey: trajectoryKeys.exportJob(started.viewer, sessionId, job.export_id),
      })
    },
    retry: false,
  })
}

const OPEN_EXPORT = new Set(["pending", "running"])

export function useExportJob(sessionId: string, exportId: string | null) {
  const scope = useAccessScope()
  return useQuery({
    queryKey: trajectoryKeys.exportJob(scope.viewer, sessionId, exportId ?? ""),
    queryFn: ({ signal }) => trajectoryApi.exportStatus(sessionId, exportId ?? "", signal),
    enabled: scope.allowed && !!exportId,
    refetchInterval: (query) =>
      OPEN_EXPORT.has((query.state.data as ExportJob | undefined)?.status ?? "pending") ? 2_000 : false,
    retry: retryUnlessDenied,
  })
}

/** Download through the authenticated API; the server re-checks admin rights on every call. */
export function useExportDownload(sessionId: string) {
  const scope = useAccessScope()
  return useMutation({
    mutationKey: [...trajectoryKeys.target(scope.viewer, sessionId), "export-download"],
    // A read: it changes nothing server-side, so there is nothing to invalidate.
    mutationFn: async (exportId: string) => {
      const request = trackRequest()
      try {
        request.check()
        const { blob, filename } = await trajectoryApi.downloadExport(sessionId, exportId, request.signal)
        // Rights lost while the bytes were travelling: do not hand them to the browser.
        request.check()
        saveBlob(blob, filename ?? `${exportId}.zip`)
      } finally {
        request.release()
      }
    },
    retry: false,
  })
}
