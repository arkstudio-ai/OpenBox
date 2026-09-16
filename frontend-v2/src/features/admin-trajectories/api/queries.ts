// Query hooks. Keys come from ./keys: viewer (with access epoch) + target +
// watermark. Pages read at a fixed watermark are immutable and never go stale;
// the header and list probe follow the moving head; protected content is
// revalidated because a deletion overrides every historical watermark. Every
// hook stops reading once access has been refused (stores/access). Intervals
// live in constants/polling.
import { useCallback, useEffect, useRef } from "react"
import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
  type UseQueryResult,
} from "@tanstack/react-query"
import { useAuthStore } from "@/shared/api/auth-store"
import { ApiError } from "@/shared/api/http"
import {
  HEADER_HINT_MIN_MS,
  headerRefreshDelay,
  listProbeDelay,
  PAYLOAD_META_REVALIDATE_MS,
  PAYLOAD_REVALIDATE_MS,
} from "../constants/polling"
import { currentAccessEpoch, useTrajectoryAccess } from "../stores/access"
import type { ExportJob, RecordExpand, Seq, SessionHeader } from "../types/protocol"
import { serializeListParams, toApiParams, type ListParams } from "../utils/params"
import { saveBlob } from "../utils/download"
import { lteSeq, toSeq } from "../utils/seq"
import { retryUnlessDenied, trackRequest } from "./access"
import { trajectoryApi } from "./endpoints"
import { LIVE, trajectoryKeys } from "./keys"
import { trajectorySocket } from "./socket"

export const SESSION_PAGE_SIZE = 50
export const RECORD_PAGE_SIZE = 100
export const SEARCH_PAGE_SIZE = 50

/** Plain text/JSON payloads above this size are offered as a download only. */
export const INLINE_PAYLOAD_LIMIT = 2 * 1024 * 1024

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

/** What a safety poll needs from a live query: when it last answered, and how to ask again. */
type LiveQuery = Pick<UseQueryResult<unknown, unknown>, "dataUpdatedAt" | "errorUpdatedAt" | "refetch">

/**
 * The safety poll of a query that follows the moving head: read it again
 * `delay` after its last answer, whatever brought that answer (a hint, focus,
 * this poll), in a hidden tab too. The watermark socket decides the delay, so
 * its opening or dropping moves the due time — measured from the last answer,
 * never from the change: a socket that keeps opening and dropping cannot
 * postpone the read forever.
 */
function useSafetyRefetch(query: LiveQuery, enabled: boolean, delay: (connected: boolean) => number): void {
  const answeredAt = Math.max(query.dataUpdatedAt, query.errorUpdatedAt)
  const { refetch } = query
  useEffect(() => {
    // Before its first answer the query is still on its first read.
    if (!enabled || !answeredAt) return
    let timer: number | null = null
    const arm = () => {
      if (timer !== null) window.clearTimeout(timer)
      const wait = answeredAt + delay(trajectorySocket.connected) - Date.now()
      // A read already on its way answers for this one.
      timer = window.setTimeout(() => void refetch({ cancelRefetch: false }), Math.max(0, wait))
    }
    const offs = [trajectorySocket.on("__connected", arm), trajectorySocket.on("__disconnected", arm)]
    arm()
    return () => {
      if (timer !== null) window.clearTimeout(timer)
      for (const off of offs) off()
    }
  }, [answeredAt, delay, enabled, refetch])
}

/**
 * Newest row for the current filters and sort, probed so the list can say
 * "updated" without reordering: again LIST_PROBE_DISCONNECTED_MS after its last
 * answer, or LIST_PROBE_CONNECTED_MS while the watermark socket is open.
 */
export function useSessionListProbe(params: ListParams, enabled: boolean) {
  const scope = useAccessScope()
  const first = { ...params, cursor: null, trail: [] }
  const probe = useQuery({
    ...sessionListQuery(scope.viewer, first, 1),
    enabled: enabled && scope.allowed,
    retry: retryUnlessDenied,
  })
  useSafetyRefetch(probe, enabled && scope.allowed, listProbeDelay)
  return probe
}

/**
 * Live header: identity and current statuses. Its statistics describe the head,
 * not a replay position. While a recording is shown, watermark hints refresh it
 * (useHeaderHintRefresh); on its own it is read again HEADER_REFRESH_CONNECTED_MS
 * after its last answer while the socket is open, HEADER_REFRESH_DISCONNECTED_MS
 * while it is not.
 */
export function useSessionHeader(sessionId: string) {
  const scope = useAccessScope()
  const header = useQuery({
    queryKey: trajectoryKeys.header(scope.viewer, sessionId, LIVE),
    queryFn: ({ signal }) => trajectoryApi.header(sessionId, undefined, signal),
    enabled: scope.allowed,
    retry: retryUnlessDenied,
  })
  useSafetyRefetch(header, scope.allowed, headerRefreshDelay)
  return header
}

/** What a watermark hint (TrajectoryWatermark) tells the live header. */
export interface HeaderHint {
  committed_seq?: string | null
  deleted?: boolean
}

/**
 * Refresh the live header when the watermark socket reports a commit for the
 * target. A hint the header already covers — the answer to a (re)subscription,
 * a duplicate — reads nothing. A streaming run announces every commit, so the
 * header is read at most once per HEADER_HINT_MIN_MS since its last answer,
 * and a trailing read always covers the latest hint. A deletion is read at once.
 */
export function useHeaderHintRefresh(sessionId: string): (hint: HeaderHint) => void {
  const { viewer } = useAccessScope()
  const client = useQueryClient()
  const timer = useRef<number | null>(null)

  useEffect(
    () => () => {
      if (timer.current !== null) window.clearTimeout(timer.current)
      timer.current = null
    },
    [client, sessionId, viewer],
  )

  return useCallback(
    (hint: HeaderHint) => {
      const queryKey = trajectoryKeys.header(viewer, sessionId, LIVE)
      const read = (cancelRefetch: boolean) => {
        timer.current = null
        void client.refetchQueries({ queryKey, exact: true, type: "active" }, { cancelRefetch })
      }
      if (hint.deleted) {
        if (timer.current !== null) window.clearTimeout(timer.current)
        read(true)
        return
      }
      if (timer.current !== null) return
      const shown = toSeq(client.getQueryData<SessionHeader>(queryKey)?.committed_seq)
      const hinted = toSeq(hint.committed_seq)
      if (shown !== null && hinted !== null && lteSeq(hinted, shown)) return
      const state = client.getQueryState(queryKey)
      const wait = HEADER_HINT_MIN_MS - (Date.now() - (state?.dataUpdatedAt ?? 0))
      // An answer already on its way may predate this hint: read again once that one is old enough.
      const delay = state?.fetchStatus === "fetching" ? Math.max(wait, HEADER_HINT_MIN_MS) : wait
      if (delay <= 0) read(false)
      else timer.current = window.setTimeout(() => read(false), delay)
    },
    [client, sessionId, viewer],
  )
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

export interface RecordDetailOptions {
  enabled: boolean
  /** `refs` keeps content-addressed values as `$ref` envelopes; only for servers with `capabilities.refs`. */
  expand?: RecordExpand
}

export function useRecordDetail(
  sessionId: string,
  throughSeq: Seq | null,
  recordId: string | null,
  options: RecordDetailOptions,
) {
  const scope = useAccessScope()
  const refs = options.expand === "refs"
  const at = throughSeq ?? ""
  const id = recordId ?? ""
  return useQuery({
    queryKey: refs
      ? trajectoryKeys.recordRefs(scope.viewer, sessionId, at, id)
      : trajectoryKeys.record(scope.viewer, sessionId, at, id),
    queryFn: ({ signal }) =>
      refs
        ? trajectoryApi.recordRefs(sessionId, id, throughSeq ?? "0", signal)
        : trajectoryApi.record(sessionId, id, throughSeq ?? "0", signal),
    enabled: options.enabled && scope.allowed && !!recordId && throughSeq !== null,
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

type PayloadAvailability = PayloadContent["availability"]
type Unavailable = Exclude<PayloadAvailability, "available">

function isTextual(mediaType: string): boolean {
  return mediaType.startsWith("text/") || mediaType.includes("json") || mediaType.includes("xml")
}

function bodyless(availability: Unavailable): PayloadContent {
  return { availability, mediaType: null, size: null, blob: null, text: null }
}

const UNAVAILABLE: Readonly<Record<number, Unavailable>> = {
  404: "pending",
  410: "deleted",
  409: "corrupt",
}

/** A `?meta=1` availability as shown content understands it; anything unrecognised hides the content. */
const META_AVAILABILITY: Readonly<Record<string, PayloadAvailability>> = {
  available: "available",
  pending: "pending",
  corrupt: "corrupt",
  deleted: "deleted",
  expired: "deleted",
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
    if (availability) return bodyless(availability)
    throw error
  }
}

/** Whether protected content is still readable at a watermark, without reading its bytes. */
export async function loadPayloadMeta(
  sessionId: string,
  payloadId: string,
  throughSeq: Seq,
  signal?: AbortSignal,
): Promise<PayloadAvailability> {
  try {
    const meta = await trajectoryApi.payloadMeta(sessionId, payloadId, throughSeq, signal)
    const availability: unknown = meta?.availability
    // Own keys only: "constructor" or "__proto__" must not pass for an answer.
    return typeof availability === "string" &&
      Object.prototype.hasOwnProperty.call(META_AVAILABILITY, availability)
      ? META_AVAILABILITY[availability]
      : "deleted"
  } catch (error) {
    const availability = error instanceof ApiError ? UNAVAILABLE[error.status] : undefined
    if (availability) return availability
    throw error
  }
}

/**
 * How shown content notices a deletion. `body` reads the bytes again every
 * PAYLOAD_REVALIDATE_MS and on focus. `meta` (servers with `capabilities.refs`)
 * reads them once, then asks `?meta=1` PAYLOAD_META_REVALIDATE_MS after the last
 * answer while the content is on screen — at once when it comes back into view
 * later than that — and whenever the tab becomes visible, and replaces the body
 * once the answer is no longer "available".
 */
export type PayloadRevalidation = "body" | "meta"

export interface PayloadOptions {
  revalidation?: PayloadRevalidation
  /** Whether the content is on screen. `meta` checks pause while it is not; `body` re-reads do not. */
  shown?: boolean
}

export function usePayload(
  sessionId: string,
  throughSeq: Seq | null,
  payloadId: string | null,
  { revalidation = "body", shown = true }: PayloadOptions = {},
) {
  const scope = useAccessScope()
  const client = useQueryClient()
  const at = throughSeq ?? ""
  const id = payloadId ?? ""
  const queryKey = trajectoryKeys.payload(scope.viewer, sessionId, at, id)
  const enabled = scope.allowed && !!payloadId && throughSeq !== null
  const byMeta = revalidation === "meta"
  const body = useQuery({
    queryKey,
    queryFn: ({ signal }) => loadPayload(sessionId, id, throughSeq ?? "0", signal),
    enabled,
    // Deletion wins over any watermark, so shown content keeps asking; no Blob
    // outlives the component that displays it.
    staleTime: 0,
    gcTime: 0,
    refetchOnMount: "always",
    refetchOnWindowFocus: !byMeta,
    refetchInterval: byMeta ? false : visibleInterval(PAYLOAD_REVALIDATE_MS, false),
    retry: retryUnlessDenied,
  })
  const meta = useQuery({
    queryKey: trajectoryKeys.payloadMeta(scope.viewer, sessionId, at, id),
    queryFn: async ({ signal }) => {
      const availability = await loadPayloadMeta(sessionId, id, throughSeq ?? "0", signal)
      // Replacing the body drops the Blob and unmounts whatever displayed it.
      if (availability !== "available" && !signal.aborted)
        client.setQueryData(queryKey, bodyless(availability))
      return availability
    },
    // Never on its own — on mount the bytes were just read. Asked on the schedule below.
    enabled: false,
    gcTime: 0,
    retry: retryUnlessDenied,
  })
  const checkAvailability = meta.refetch
  const checking = byMeta && enabled && shown && body.data?.availability === "available"
  // When availability was last learned: the bytes, or a check that answered or failed.
  const checkedAt = Math.max(body.dataUpdatedAt, meta.dataUpdatedAt, meta.errorUpdatedAt)
  useEffect(() => {
    if (!checking) return
    const check = () => {
      if (!hidden()) void checkAvailability()
    }
    // Due an interval after the last answer, so content that scrolls out of view
    // and back neither postpones the check nor repeats it early.
    const timer = window.setTimeout(check, Math.max(0, checkedAt + PAYLOAD_META_REVALIDATE_MS - Date.now()))
    document.addEventListener("visibilitychange", check)
    return () => {
      window.clearTimeout(timer)
      document.removeEventListener("visibilitychange", check)
    }
  }, [checkAvailability, checkedAt, checking])
  return body
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
