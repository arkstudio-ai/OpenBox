// Deterministic stand-in for /api/admin/trajectories and the read-only
// watermark socket. Every answer is computed from real projector state at the
// watermark the request names — the same reducer the page runs, fed the same
// events — so a component that asks for the wrong position sees the wrong
// data, exactly as it would against the server. This is fixture evidence for
// how the production components use the protocol; it proves nothing about the
// backend's authorization or storage.
//
// Logs keep method, path and non-secret query parameters only. No header is
// ever copied into a log, so no Authorization value or ticket reaches evidence.
import type { Page, Request, Route, WebSocketRoute } from "@playwright/test"
import {
  emptyState,
  eventsForRecord,
  reduceMany,
} from "../../src/features/admin-trajectories/utils/projector"
import { agents, statistics } from "../../src/features/admin-trajectories/utils/statistics"
import type {
  ProjectionState,
  RecordSummary,
  SessionHeader,
  SessionRow,
  TrajectoryEvent,
  TrajectoryRecord,
} from "../../src/features/admin-trajectories/types/protocol"

export const TRAJECTORY_API = "/api/admin/trajectories"
export const TRAJECTORY_SOCKET = "/ws/admin/trajectories"
/** The backend's default TRAJECTORY_CHECKPOINT_INTERVAL. */
const CHECKPOINT_INTERVAL = 1000
const LOGGED_PARAMS = [
  "through_seq",
  "after_seq",
  "until_seq",
  "at_seq",
  "limit",
  "sort",
  "kind",
  "status",
  "agent_id",
  "q",
  "user_query",
  "user_id",
  "workspace_id",
  "recording_status",
  "include_unrecorded",
  "activity_from",
  "activity_to",
] as const

export interface SessionMeta {
  session_id: string
  title: string | null
  owner: { user_id: string; username: string | null; email: string | null }
  workspace: { id: string | null; name: string | null } | null
  running_status: string | null
  recording_status: string
  last_activity_at: string | null
  model: string | null
  agent: string | null
  /** false: an ordinary session that never started recording (header only). */
  recorded: boolean
}

export type PayloadState = "available" | "deleted" | "corrupt"

export interface PayloadSpec {
  id: string
  body: Buffer
  mediaType: string
  /** First seq at which the payload exists; earlier watermarks answer 404. */
  firstSeq: number
  state?: PayloadState
}

export interface FixtureSessionSpec {
  meta: SessionMeta
  events: TrajectoryEvent[]
  payloads?: PayloadSpec[]
  /** Serve checkpoints every 1000 events, as the backend does for long sessions. */
  checkpoints?: boolean
}

export interface RequestLog {
  at: number
  method: string
  path: string
  params: Record<string, string>
  /** An opaque list cursor or record-page `before` was sent (its value is not kept). */
  paged: boolean
  status: number
}

export interface PayloadRead {
  at: number
  sessionId: string
  payloadId: string
  throughSeq: number
  status: number
}

export interface SocketLog {
  at: number
  kind: "open" | "client" | "close"
  path: string
  type?: string
  sessionId?: string
  code?: number
}

interface Reply {
  status: number
  json?: unknown
  raw?: string | Buffer
  contentType?: string
  headers?: Record<string, string>
}

interface Socket {
  ws: WebSocketRoute
  subscriptions: Set<string>
}

interface ExportRecord {
  exportId: string
  sessionId: string
  throughSeq: string
  polls: number
}

const json = (status: number, body: unknown): Reply => ({ status, json: body })
const detail = (status: number, message: string): Reply => json(status, { detail: message })

function toInt(value: string | null): number | null {
  return value !== null && /^\d+$/.test(value) ? Number(value) : null
}

function opaque(value: unknown): string {
  return Buffer.from(JSON.stringify(value)).toString("base64url")
}

function unopaque<T>(value: string): T | null {
  try {
    return JSON.parse(Buffer.from(value, "base64url").toString("utf8")) as T
  } catch {
    return null
  }
}

function digest(text: string): string {
  let hash = 5381
  for (let index = 0; index < text.length; index += 1) hash = ((hash * 33) ^ text.charCodeAt(index)) >>> 0
  return hash.toString(36)
}

/** List rows never carry the captured body (repository.list_records). */
function summary(record: TrajectoryRecord): RecordSummary {
  const copy: Partial<TrajectoryRecord> = { ...record }
  delete copy.data
  delete copy.blocks
  return copy as RecordSummary
}

function bySeq(a: TrajectoryRecord, b: TrajectoryRecord): number {
  const order = Number(a.start_seq) - Number(b.start_seq)
  if (order) return order
  return a.record_id < b.record_id ? -1 : a.record_id > b.record_id ? 1 : 0
}

class FixtureSession {
  readonly events: TrajectoryEvent[] = []
  readonly payloads = new Map<string, PayloadSpec & { state: PayloadState }>()
  gone = false
  private readonly anchors = new Map<number, ProjectionState>([[0, emptyState()]])
  private readonly positions = new Map<number, ProjectionState>()
  private readonly ordered = new Map<number, TrajectoryRecord[]>()
  private readonly stateBodies = new Map<number, string>()

  constructor(
    readonly meta: SessionMeta,
    events: readonly TrajectoryEvent[],
    payloads: readonly PayloadSpec[],
    readonly checkpoints: boolean,
  ) {
    this.append(events)
    for (const payload of payloads)
      this.payloads.set(payload.id, { ...payload, state: payload.state ?? "available" })
  }

  get head(): number {
    return this.events.length
  }

  append(events: readonly TrajectoryEvent[]): void {
    for (const event of events) {
      if (Number(event.seq) !== this.events.length + 1)
        throw new Error(`${this.meta.session_id}: seq ${event.seq} breaks the contiguous log`)
      this.events.push(event)
    }
    // Append-only log: anchors at or below the previous head stay exact.
    this.positions.clear()
    this.ordered.clear()
  }

  stateAt(seq: number): ProjectionState {
    const at = Math.max(0, Math.min(seq, this.head))
    const anchor = at - (at % CHECKPOINT_INTERVAL)
    let base = this.anchors.get(anchor)
    if (!base) {
      base = reduceMany(
        this.stateAt(anchor - CHECKPOINT_INTERVAL),
        this.events.slice(anchor - CHECKPOINT_INTERVAL, anchor),
      )
      this.anchors.set(anchor, base)
    }
    if (at === anchor) return base
    const known = this.positions.get(at)
    if (known) return known
    const state = reduceMany(base, this.events.slice(anchor, at))
    if (this.positions.size >= 64) this.positions.delete(this.positions.keys().next().value as number)
    this.positions.set(at, state)
    return state
  }

  stateBody(at: number): string {
    let body = this.stateBodies.get(at)
    if (body === undefined) {
      body = JSON.stringify(this.stateAt(at))
      this.stateBodies.set(at, body)
    }
    return body
  }

  recordsAt(at: number): TrajectoryRecord[] {
    let rows = this.ordered.get(at)
    if (!rows) {
      rows = Object.values(this.stateAt(at).records).sort(bySeq)
      if (this.ordered.size >= 8) this.ordered.delete(this.ordered.keys().next().value as number)
      this.ordered.set(at, rows)
    }
    return rows
  }

  header(at: number = this.head): SessionHeader {
    const state = this.stateAt(at)
    const { meta } = this
    return {
      session_id: meta.session_id,
      user_id: meta.owner.user_id,
      trajectory_id: meta.recorded ? `trj_${meta.session_id}` : null,
      title: meta.title,
      owner: meta.owner,
      workspace: meta.workspace,
      workspace_id: meta.workspace?.id ?? null,
      running_status: meta.running_status,
      recording_status: meta.recording_status,
      coverage_start: state.coverage_start,
      last_activity_at: meta.last_activity_at,
      model: meta.model,
      agent: meta.agent,
      committed_seq: String(this.head),
      projected_through_seq: String(this.head),
      through_seq: String(at),
      statistics: statistics(state),
      agents: agents(state),
      capabilities: { recording: meta.recorded, admin_read: true, export: meta.recorded },
      projector_version: 1,
      unsupported_events: state.unsupported_events,
    }
  }

  row(): SessionRow {
    const header = this.header()
    return {
      session_id: header.session_id,
      user_id: header.user_id,
      trajectory_id: header.trajectory_id,
      title: header.title,
      owner: header.owner,
      workspace: header.workspace,
      workspace_id: header.workspace_id,
      running_status: header.running_status,
      recording_status: header.recording_status,
      coverage_start: header.coverage_start,
      last_activity_at: header.last_activity_at,
      model: header.model,
      agent: header.agent,
      committed_seq: header.committed_seq,
      projected_through_seq: header.projected_through_seq,
      through_seq: header.through_seq,
      statistics: { ...header.statistics, duration_ms: null },
    }
  }
}

export class TrajectoryFixtureServer {
  readonly requests: RequestLog[] = []
  readonly payloadReads: PayloadRead[] = []
  readonly socketLog: SocketLog[] = []
  /** Requests this read-only viewer should never make (other APIs, sockets, frames). */
  readonly unexpected: string[] = []
  /** Reads naming a watermark past the committed head. */
  readonly futureReads: string[] = []
  private readonly sessions = new Map<string, FixtureSession>()
  private readonly sockets = new Set<Socket>()
  private readonly exports = new Map<string, ExportRecord>()
  private denial: 401 | 403 | null = null

  add(spec: FixtureSessionSpec): this {
    this.sessions.set(
      spec.meta.session_id,
      new FixtureSession(spec.meta, spec.events, spec.payloads ?? [], spec.checkpoints ?? false),
    )
    return this
  }

  head(sessionId: string): number {
    return this.require(sessionId).head
  }

  /** Commit more events; by default the socket then announces the new head. */
  append(sessionId: string, events: readonly TrajectoryEvent[], announce = true): void {
    this.require(sessionId).append(events)
    if (announce) this.announce(sessionId)
  }

  announce(sessionId: string): void {
    const session = this.require(sessionId)
    for (const socket of this.sockets) {
      if (socket.subscriptions.has(sessionId))
        this.send(socket.ws, { type: "trajectory.available", data: this.notice(session) })
    }
  }

  /** Every later trajectory read and socket is refused, like an account that lost the admin role. */
  deny(status: 401 | 403): void {
    this.denial = status
  }

  /** Close every open watermark socket with a server close code (e.g. 4403). */
  closeSockets(code: number): void {
    for (const socket of [...this.sockets]) {
      this.sockets.delete(socket)
      this.socketLog.push({ at: Date.now(), kind: "close", path: TRAJECTORY_SOCKET, code })
      void socket.ws.close({ code, reason: "fixture" })
    }
  }

  openSockets(): number {
    return this.sockets.size
  }

  deleteSession(sessionId: string): void {
    this.require(sessionId).gone = true
  }

  setPayloadState(sessionId: string, payloadId: string, state: PayloadState): void {
    const payload = this.require(sessionId).payloads.get(payloadId)
    if (!payload) throw new Error(`unknown payload ${payloadId}`)
    payload.state = state
  }

  /** The projection the server would answer with at `seq` (for parity checks against golden files). */
  stateAt(sessionId: string, seq: number): ProjectionState {
    return this.require(sessionId).stateAt(seq)
  }

  async install(page: Page): Promise<void> {
    await page.route(
      (url) => url.pathname.startsWith("/api/"),
      (route) => this.handle(route),
    )
    await page.routeWebSocket(
      (url) => url.pathname.startsWith("/ws/"),
      (ws) => this.connect(ws),
    )
  }

  private require(sessionId: string): FixtureSession {
    const session = this.sessions.get(sessionId)
    if (!session) throw new Error(`unknown fixture session ${sessionId}`)
    return session
  }

  private async handle(route: Route): Promise<void> {
    const request = route.request()
    const url = new URL(request.url())
    const method = request.method()
    const cors = {
      "access-control-allow-origin": request.headers().origin ?? "*",
      "access-control-allow-credentials": "true",
      "access-control-allow-headers": "authorization, content-type, x-workspace-id",
      "access-control-allow-methods": "GET, POST, OPTIONS",
      "access-control-expose-headers": "content-disposition",
    }
    if (method === "OPTIONS") {
      await route.fulfill({ status: 204, headers: cors }).catch(() => undefined)
      return
    }
    let reply: Reply
    try {
      reply = this.dispatch(method, url, request)
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error)
      this.unexpected.push(`500 ${method} ${url.pathname}: ${message}`)
      reply = detail(500, "fixture error")
    }
    const params = Object.fromEntries(
      LOGGED_PARAMS.filter((key) => url.searchParams.has(key)).map((key) => [
        key,
        url.searchParams.get(key) ?? "",
      ]),
    )
    this.requests.push({
      at: Date.now(),
      method,
      path: url.pathname,
      params,
      paged: url.searchParams.has("cursor") || url.searchParams.has("before"),
      status: reply.status,
    })
    await route
      .fulfill({
        status: reply.status,
        headers: {
          ...cors,
          "cache-control": "no-store",
          "content-type": reply.contentType ?? "application/json",
          ...reply.headers,
        },
        body: reply.raw ?? JSON.stringify(reply.json ?? null),
      })
      // The page may have navigated or closed while the answer was prepared.
      .catch(() => undefined)
  }

  private dispatch(method: string, url: URL, request: Request): Reply {
    const path = url.pathname
    if (path !== TRAJECTORY_API && !path.startsWith(`${TRAJECTORY_API}/`)) {
      this.unexpected.push(`${method} ${path}`)
      return detail(404, "fixture: outside the trajectory API")
    }
    if (this.denial !== null)
      return detail(this.denial, this.denial === 401 ? "not authenticated" : "admin role required")
    const rest = path.slice(TRAJECTORY_API.length)
    if (method === "POST" && rest === "/ticket") return json(200, { ticket: "fixture-ticket" })
    if (method === "GET" && rest === "/sessions") return this.list(url.searchParams)
    const match = /^\/sessions\/([^/]+)(\/.*)?$/.exec(rest)
    if (!match) {
      this.unexpected.push(`${method} ${path}`)
      return detail(404, "fixture: not served")
    }
    const session = this.sessions.get(decodeURIComponent(match[1]))
    if (!session) return detail(404, "session not found")
    if (session.gone) return detail(410, "session deleted")
    return this.sessionResource(session, match[2] ?? "", url, request)
  }

  private sessionResource(session: FixtureSession, sub: string, url: URL, request: Request): Reply {
    const method = request.method()
    const { pathname: path, searchParams: query } = url
    if (method === "GET" && sub === "")
      return json(200, session.header(this.watermark(session, query.get("through_seq"), path)))
    // A session that never recorded has a header and nothing else; reading it initialises nothing.
    if (!session.meta.recorded) return detail(404, "trajectory not recorded")
    if (method === "POST" && sub === "/export") return this.createExport(session, request)
    const reply = method === "GET" ? this.read(session, sub, query, path) : null
    if (reply) return reply
    this.unexpected.push(`${method} ${path}`)
    return detail(404, "fixture: not served")
  }

  private read(session: FixtureSession, sub: string, query: URLSearchParams, path: string): Reply | null {
    const tail = (prefix: string) =>
      sub.startsWith(prefix) ? decodeURIComponent(sub.slice(prefix.length)) : null
    if (sub === "/records") return this.records(session, query, path)
    if (sub === "/events") return this.eventPage(session, query, path)
    if (sub === "/checkpoint") return this.checkpoint(session, query, path)
    if (sub === "/search") return this.search(session, query, path)
    const recordId = tail("/records/")
    if (recordId !== null) return this.record(session, recordId, query, path)
    const payloadId = tail("/payloads/")
    if (payloadId !== null) return this.payload(session, payloadId, query, path)
    const exportMatch = /^\/exports\/([^/]+)(\/download)?$/.exec(sub)
    if (exportMatch)
      return this.exportJob(session, decodeURIComponent(exportMatch[1]), exportMatch[2] !== undefined)
    return null
  }

  /** Requested watermark, clamped to the head; asking past the head is logged as a finding. */
  private watermark(session: FixtureSession, raw: string | null, path: string): number {
    const value = toInt(raw)
    if (value !== null && value > session.head)
      this.futureReads.push(`${path} through=${value} head=${session.head}`)
    return value === null ? session.head : Math.min(value, session.head)
  }

  private list(query: URLSearchParams): Reply {
    const filters = [...query.entries()]
      .filter(([key]) => key !== "cursor" && key !== "limit")
      .sort(([a], [b]) => a.localeCompare(b))
    const bound = digest(JSON.stringify(filters))
    const text = (key: string) => (query.get(key) ?? "").trim().toLowerCase()
    let rows = [...this.sessions.values()].filter(
      (session) => !session.gone && (query.get("include_unrecorded") === "true" || session.meta.recorded),
    )
    const owner = text("user_query")
    if (owner)
      rows = rows.filter(({ meta }) =>
        [meta.owner.username, meta.owner.email].some((value) => value?.toLowerCase().includes(owner)),
      )
    const exact: Array<[string, (meta: SessionMeta) => string | null | undefined]> = [
      ["user_id", (meta) => meta.owner.user_id],
      ["workspace_id", (meta) => meta.workspace?.id],
      ["status", (meta) => meta.running_status],
      ["recording_status", (meta) => meta.recording_status],
    ]
    for (const [key, read] of exact) {
      const wanted = query.get(key)
      if (wanted) rows = rows.filter(({ meta }) => read(meta) === wanted)
    }
    const q = text("q")
    if (q)
      rows = rows.filter(
        ({ meta }) =>
          meta.session_id.toLowerCase().includes(q) || (meta.title ?? "").toLowerCase().includes(q),
      )
    const from = query.get("activity_from")
    const to = query.get("activity_to")
    if (from) rows = rows.filter(({ meta }) => (meta.last_activity_at ?? "") >= from)
    if (to) rows = rows.filter(({ meta }) => (meta.last_activity_at ?? "") <= to)
    const ascending = query.get("sort") === "last_activity_asc"
    rows.sort((a, b) => {
      const x = a.meta.last_activity_at ?? ""
      const y = b.meta.last_activity_at ?? ""
      const order = x < y ? -1 : x > y ? 1 : a.meta.session_id.localeCompare(b.meta.session_id)
      return ascending ? order : -order
    })
    let offset = 0
    const cursor = query.get("cursor")
    if (cursor) {
      const decoded = unopaque<{ o: number; f: string }>(cursor)
      if (!decoded || decoded.f !== bound) return detail(400, "cursor does not belong to these filters")
      offset = decoded.o
    }
    const limit = Math.min(toInt(query.get("limit")) ?? 50, 200)
    const more = offset + limit < rows.length
    return json(200, {
      items: rows.slice(offset, offset + limit).map((session) => session.row()),
      next_cursor: more ? opaque({ o: offset + limit, f: bound }) : null,
      has_more: more,
    })
  }

  private records(session: FixtureSession, query: URLSearchParams, path: string): Reply {
    const through = this.watermark(session, query.get("through_seq"), path)
    const kind = query.get("kind")
    const status = query.get("status")
    const agent = query.get("agent_id")
    const items = session
      .recordsAt(through)
      .filter(
        (record) =>
          (!kind || record.kind === kind) &&
          (!status || record.status === status) &&
          (!agent || record.agent_id === agent),
      )
    let end = items.length
    const before = query.get("before")
    if (before) {
      const decoded = unopaque<{ i: number; h: number }>(before)
      if (!decoded || decoded.h !== through) return detail(400, "cursor belongs to another watermark")
      end = Math.min(decoded.i, items.length)
    }
    const start = Math.max(0, end - Math.min(toInt(query.get("limit")) ?? 100, 500))
    return json(200, {
      items: items.slice(start, end).map(summary),
      next_cursor: start > 0 ? opaque({ i: start, h: through }) : null,
      has_more: start > 0,
      through_seq: String(through),
      projector_version: 1,
      unsupported_events: session.stateAt(through).unsupported_events,
    })
  }

  private record(session: FixtureSession, recordId: string, query: URLSearchParams, path: string): Reply {
    const through = this.watermark(session, query.get("through_seq"), path)
    const record = session.stateAt(through).records[recordId]
    if (!record) return detail(404, "record does not exist at this watermark")
    const events = eventsForRecord(record, session.events.slice(0, through))
    return json(200, { record: { ...record, events }, through_seq: String(through), projector_version: 1 })
  }

  private eventPage(session: FixtureSession, query: URLSearchParams, path: string): Reply {
    const head = session.head
    const after = toInt(query.get("after_seq")) ?? 0
    const until = query.has("until_seq") ? this.watermark(session, query.get("until_seq"), path) : head
    const end = Math.min(until, after + Math.min(toInt(query.get("limit")) ?? 500, 500))
    const events = after < end ? session.events.slice(after, end) : []
    return json(200, {
      events,
      from_seq: String(after),
      through_seq: String(events.length ? end : after),
      until_seq: String(until),
      has_more: Math.max(end, after) < until,
      committed_seq: String(head),
    })
  }

  private checkpoint(session: FixtureSession, query: URLSearchParams, path: string): Reply {
    const head = session.head
    const at = query.has("at_seq") ? this.watermark(session, query.get("at_seq"), path) : head
    const anchor = at - (at % CHECKPOINT_INTERVAL)
    if (!session.checkpoints || anchor === 0)
      return json(200, { checkpoint: null, through_seq: String(head) })
    return {
      status: 200,
      raw: `{"checkpoint":{"through_seq":"${anchor}","projector_version":1,"state":${session.stateBody(anchor)},"digest":null},"through_seq":"${head}"}`,
    }
  }

  private search(session: FixtureSession, query: URLSearchParams, path: string): Reply {
    const through = this.watermark(session, query.get("through_seq"), path)
    const needle = (query.get("q") ?? "").trim().toLowerCase()
    const hits = needle
      ? session
          .recordsAt(through)
          .filter((record) =>
            [record.title, record.preview, record.result_preview].some((value) =>
              value?.toLowerCase().includes(needle),
            ),
          )
          .map((record) => ({
            record_id: record.record_id,
            seq: record.start_seq,
            kind: record.kind,
            preview: record.preview ?? record.title,
          }))
      : []
    const cursor = query.get("cursor")
    const offset = cursor ? (unopaque<{ o: number }>(cursor)?.o ?? 0) : 0
    const limit = Math.min(toInt(query.get("limit")) ?? 50, 200)
    const more = offset + limit < hits.length
    return json(200, {
      items: hits.slice(offset, offset + limit),
      next_cursor: more ? opaque({ o: offset + limit }) : null,
      has_more: more,
      through_seq: String(through),
    })
  }

  private payload(session: FixtureSession, payloadId: string, query: URLSearchParams, path: string): Reply {
    const through = this.watermark(session, query.get("through_seq"), path)
    const payload = session.payloads.get(payloadId)
    const status =
      !payload || through < payload.firstSeq
        ? 404
        : payload.state === "deleted"
          ? 410
          : payload.state === "corrupt"
            ? 409
            : 200
    this.payloadReads.push({
      at: Date.now(),
      sessionId: session.meta.session_id,
      payloadId,
      throughSeq: through,
      status,
    })
    if (!payload || status !== 200)
      return detail(
        status,
        status === 410
          ? "payload deleted"
          : status === 409
            ? "payload corrupt"
            : "payload does not exist at this watermark",
      )
    return { status, raw: payload.body, contentType: payload.mediaType }
  }

  private createExport(session: FixtureSession, request: Request): Reply {
    let throughSeq = String(session.head)
    try {
      const body = request.postDataJSON() as { through_seq?: unknown } | null
      if (typeof body?.through_seq === "string") throughSeq = body.through_seq
    } catch {
      // No JSON body: the server exports at its head.
    }
    const exportId = `exp_${this.exports.size + 1}`
    this.exports.set(exportId, { exportId, sessionId: session.meta.session_id, throughSeq, polls: 0 })
    return json(202, {
      export_id: exportId,
      status: "pending",
      through_seq: throughSeq,
      error: null,
      download_url: null,
    })
  }

  private exportJob(session: FixtureSession, exportId: string, download: boolean): Reply {
    const job = this.exports.get(exportId)
    if (!job || job.sessionId !== session.meta.session_id) return detail(404, "export not found")
    if (download) {
      // An empty ZIP (end-of-central-directory record only).
      const zip = Buffer.concat([Buffer.from([0x50, 0x4b, 0x05, 0x06]), Buffer.alloc(18)])
      return {
        status: 200,
        raw: zip,
        contentType: "application/zip",
        headers: { "content-disposition": `attachment; filename="${exportId}.zip"` },
      }
    }
    job.polls += 1
    const done = job.polls > 1
    return json(200, {
      export_id: exportId,
      status: done ? "completed" : "running",
      through_seq: job.throughSeq,
      error: null,
      download_url: done
        ? `${TRAJECTORY_API}/sessions/${encodeURIComponent(job.sessionId)}/exports/${exportId}/download`
        : null,
    })
  }

  private notice(session: FixtureSession) {
    return {
      user_id: session.meta.owner.user_id,
      owner_user_id: session.meta.owner.user_id,
      session_id: session.meta.session_id,
      trajectory_id: `trj_${session.meta.session_id}`,
      committed_seq: String(session.head),
    }
  }

  private send(ws: WebSocketRoute, frame: unknown): void {
    try {
      ws.send(JSON.stringify(frame))
    } catch {
      // Socket already closed by the page.
    }
  }

  private connect(ws: WebSocketRoute): void {
    const path = new URL(ws.url()).pathname
    this.socketLog.push({ at: Date.now(), kind: "open", path })
    if (path !== TRAJECTORY_SOCKET) {
      this.unexpected.push(`WS ${path}`)
      void ws.close({ code: 4404, reason: "fixture: not served" })
      return
    }
    if (this.denial !== null) {
      void ws.close({ code: this.denial === 401 ? 4401 : 4403, reason: "fixture: refused" })
      return
    }
    const socket: Socket = { ws, subscriptions: new Set() }
    this.sockets.add(socket)
    ws.onMessage((message) => this.onFrame(socket, message))
    ws.onClose((code) => {
      if (!this.sockets.delete(socket)) return
      this.socketLog.push({ at: Date.now(), kind: "close", path, code })
    })
  }

  private onFrame(socket: Socket, message: string | Buffer): void {
    let frame: Record<string, unknown>
    try {
      frame = JSON.parse(message.toString()) as Record<string, unknown>
    } catch {
      this.unexpected.push("WS non-JSON client frame")
      return
    }
    const type = typeof frame.type === "string" ? frame.type : "unknown"
    const sessionId = typeof frame.session_id === "string" ? frame.session_id : undefined
    this.socketLog.push({ at: Date.now(), kind: "client", path: TRAJECTORY_SOCKET, type, sessionId })
    if (type === "ping") {
      this.send(socket.ws, { type: "pong", data: {} })
    } else if (type === "unsubscribe" && sessionId) {
      socket.subscriptions.delete(sessionId)
      this.send(socket.ws, { type: "unsubscribed", data: { session_id: sessionId } })
    } else if (type === "subscribe" && sessionId) {
      const session = this.sessions.get(sessionId)
      if (!session || session.gone || !session.meta.recorded) {
        this.send(socket.ws, { type: "error", data: { code: "not_found", session_id: sessionId } })
        return
      }
      socket.subscriptions.add(sessionId)
      this.send(socket.ws, { type: "subscribed", data: this.notice(session) })
    } else {
      this.unexpected.push(`WS client frame ${type}`)
    }
  }
}
