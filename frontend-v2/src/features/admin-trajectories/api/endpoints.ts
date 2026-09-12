// The only transport this feature has: `/api/admin/trajectories/*`. Every call
// is a read except `createExport`. Nothing here can reach the chat, tool,
// permission, question, cancel, sandbox or attachment APIs — the inspector
// shows another person's session and must not be able to act on it.
import { http, requestBlob, type BlobResponse } from "@/shared/api/http"
import type {
  CheckpointResponse,
  EventPage,
  ExportJob,
  RecordDetail,
  RecordPage,
  SearchPage,
  SessionHeader,
  SessionPage,
  Seq,
} from "../types/protocol"

export const TRAJECTORY_API = "/api/admin/trajectories"

type Params = Record<string, string | number | boolean | null | undefined>

export function queryString(params: Params): string {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "" || value === false) continue
    search.set(key, String(value))
  }
  const text = search.toString()
  return text ? `?${text}` : ""
}

function sessionPath(sessionId: string, suffix = ""): string {
  return `${TRAJECTORY_API}/sessions/${encodeURIComponent(sessionId)}${suffix}`
}

export interface SessionListParams {
  user_id?: string
  user_query?: string
  q?: string
  workspace_id?: string
  status?: string
  recording_status?: string
  activity_from?: string
  activity_to?: string
  include_unrecorded?: boolean
  /** Server-side order; cursors are bound to it. */
  sort?: "last_activity_desc" | "last_activity_asc"
  cursor?: string
  limit?: number
}

export interface RecordPageParams {
  throughSeq: Seq
  before?: string | null
  limit?: number
  kind?: string
  status?: string
  agentId?: string
}

export interface EventPageParams {
  afterSeq: Seq
  /** Omit to read up to the head committed when the server answers. */
  untilSeq?: Seq
  limit?: number
}

export interface SearchParams {
  q: string
  throughSeq: Seq
  cursor?: string | null
  limit?: number
}

const withSignal = (signal?: AbortSignal): RequestInit => (signal ? { signal } : {})

export const trajectoryApi = {
  listSessions: (params: SessionListParams, signal?: AbortSignal) =>
    http.get<SessionPage>(`${TRAJECTORY_API}/sessions${queryString({ ...params })}`, withSignal(signal)),

  header: (sessionId: string, throughSeq?: Seq, signal?: AbortSignal) =>
    http.get<SessionHeader>(
      sessionPath(sessionId, queryString({ through_seq: throughSeq })),
      withSignal(signal),
    ),

  records: (sessionId: string, params: RecordPageParams, signal?: AbortSignal) =>
    http.get<RecordPage>(
      sessionPath(
        sessionId,
        `/records${queryString({
          through_seq: params.throughSeq,
          before: params.before,
          limit: params.limit,
          kind: params.kind,
          status: params.status,
          agent_id: params.agentId,
        })}`,
      ),
      withSignal(signal),
    ),

  record: (sessionId: string, recordId: string, throughSeq: Seq, signal?: AbortSignal) =>
    http.get<RecordDetail>(
      sessionPath(
        sessionId,
        `/records/${encodeURIComponent(recordId)}${queryString({ through_seq: throughSeq })}`,
      ),
      withSignal(signal),
    ),

  events: (sessionId: string, params: EventPageParams, signal?: AbortSignal) =>
    http.get<EventPage>(
      sessionPath(
        sessionId,
        `/events${queryString({ after_seq: params.afterSeq, until_seq: params.untilSeq, limit: params.limit, include_data: "true" })}`,
      ),
      withSignal(signal),
    ),

  /** Without `atSeq` the server answers at its committed head and reports that head. */
  checkpoint: (sessionId: string, atSeq?: Seq, signal?: AbortSignal) =>
    http.get<CheckpointResponse>(
      sessionPath(sessionId, `/checkpoint${queryString({ at_seq: atSeq })}`),
      withSignal(signal),
    ),

  search: (sessionId: string, params: SearchParams, signal?: AbortSignal) =>
    http.get<SearchPage>(
      sessionPath(
        sessionId,
        `/search${queryString({ q: params.q, through_seq: params.throughSeq, cursor: params.cursor, limit: params.limit })}`,
      ),
      withSignal(signal),
    ),

  payload: (
    sessionId: string,
    payloadId: string,
    throughSeq: Seq,
    signal?: AbortSignal,
  ): Promise<BlobResponse> =>
    requestBlob(
      sessionPath(
        sessionId,
        `/payloads/${encodeURIComponent(payloadId)}${queryString({ through_seq: throughSeq })}`,
      ),
      withSignal(signal),
    ),

  /** The one persisted admin action: build a replay package fixed at H. */
  createExport: (sessionId: string, throughSeq: Seq, signal?: AbortSignal) =>
    http.post<ExportJob>(sessionPath(sessionId, "/export"), { through_seq: throughSeq }, withSignal(signal)),

  exportStatus: (sessionId: string, exportId: string, signal?: AbortSignal) =>
    http.get<ExportJob>(
      sessionPath(sessionId, `/exports/${encodeURIComponent(exportId)}`),
      withSignal(signal),
    ),

  downloadExport: (sessionId: string, exportId: string, signal?: AbortSignal) =>
    requestBlob(
      sessionPath(sessionId, `/exports/${encodeURIComponent(exportId)}/download`),
      withSignal(signal),
    ),
}
