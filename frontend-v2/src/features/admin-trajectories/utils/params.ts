// URL state (ENGINEERING_SPEC §8.4). The session list keeps filters, sort, the
// page cursor and the cursors behind it; the detail page keeps its record
// filters, selection and replay position, plus a sanitised copy of the list
// query so "back to list" restores exactly the page the admin came from.
//
// Server cursors are opaque and bound to filters + sort: they are carried
// byte-for-byte or refused, never trimmed — a shortened cursor is a different
// (invalid) cursor.
import type { SessionListParams } from "../api/endpoints"
import type { Seq } from "../types/protocol"
import { toSeq } from "./seq"

export const SESSION_SORTS = ["last_activity_desc", "last_activity_asc"] as const
export type SessionSort = (typeof SESSION_SORTS)[number]
export const DEFAULT_SORT: SessionSort = "last_activity_desc"

export interface ListParams {
  userQuery: string
  userId: string
  q: string
  workspaceId: string
  status: string
  recording: string
  /** ISO instants. */
  from: string
  to: string
  includeUnrecorded: boolean
  sort: SessionSort
  cursor: string | null
  /** Cursors of the pages before this one; `null` is the first page. */
  trail: ReadonlyArray<string | null>
}

export const EMPTY_LIST_PARAMS: ListParams = {
  userQuery: "",
  userId: "",
  q: "",
  workspaceId: "",
  status: "",
  recording: "",
  from: "",
  to: "",
  includeUnrecorded: false,
  sort: DEFAULT_SORT,
  cursor: null,
  trail: [],
}

const TEXT_KEYS = [
  ["userQuery", "owner"],
  ["userId", "user"],
  ["q", "q"],
  ["workspaceId", "workspace"],
  ["status", "status"],
  ["recording", "recording"],
  ["from", "from"],
  ["to", "to"],
] as const

// "." is outside the base64url alphabet the server's cursors use.
const FIRST_PAGE = "."
const MAX_TEXT = 200
/** Generous bound on an opaque cursor; anything longer is refused outright. */
export const MAX_CURSOR = 16_384
const CURSOR = /^[A-Za-z0-9_-]+$/

function clean(value: string | null): string {
  return (value ?? "").trim().slice(0, MAX_TEXT)
}

function isInstant(value: string): boolean {
  return value === "" || !Number.isNaN(Date.parse(value))
}

/** An opaque cursor exactly as received, or null when it cannot be one. */
export function opaqueCursor(value: string | null | undefined): string | null {
  if (!value || value.length > MAX_CURSOR || !CURSOR.test(value)) return null
  return value
}

function parseTrail(value: string | null): Array<string | null> | null {
  if (!value) return []
  const items = value.split(",")
  const trail: Array<string | null> = []
  for (const item of items) {
    if (item === FIRST_PAGE) {
      trail.push(null)
      continue
    }
    const cursor = opaqueCursor(item)
    if (cursor === null) return null
    trail.push(cursor)
  }
  return trail
}

export function parseListParams(search: URLSearchParams): ListParams {
  const params: ListParams = { ...EMPTY_LIST_PARAMS }
  for (const [field, key] of TEXT_KEYS) params[field] = clean(search.get(key))
  if (!isInstant(params.from)) params.from = ""
  if (!isInstant(params.to)) params.to = ""
  params.includeUnrecorded = search.get("unrecorded") === "1"
  const sort = search.get("sort")
  params.sort = (SESSION_SORTS as readonly string[]).includes(sort ?? "")
    ? (sort as SessionSort)
    : DEFAULT_SORT
  const rawCursor = search.get("cursor")
  const cursor = opaqueCursor(rawCursor)
  const trail = parseTrail(search.get("trail"))
  // A damaged cursor or trail cannot be repaired; restart paging on the same filters.
  if ((rawCursor && cursor === null) || trail === null) return params
  params.cursor = cursor
  params.trail = trail
  return params
}

/** Canonical query string (no leading "?"); defaults are omitted. */
export function serializeListParams(params: ListParams): string {
  const search = new URLSearchParams()
  for (const [field, key] of TEXT_KEYS) if (params[field]) search.set(key, params[field])
  if (params.includeUnrecorded) search.set("unrecorded", "1")
  if (params.sort !== DEFAULT_SORT) search.set("sort", params.sort)
  if (params.cursor) search.set("cursor", params.cursor)
  if (params.trail.length) search.set("trail", params.trail.map((item) => item ?? FIRST_PAGE).join(","))
  return search.toString()
}

/** Filters or sort changed: the old cursors belong to another list, so paging restarts. */
export function withFilters(params: ListParams, patch: Partial<ListParams>): ListParams {
  return { ...params, ...patch, cursor: null, trail: [] }
}

export function nextPage(params: ListParams, nextCursor: string): ListParams {
  return { ...params, cursor: nextCursor, trail: [...params.trail, params.cursor] }
}

export function previousPage(params: ListParams): ListParams {
  if (!params.trail.length) return params
  return { ...params, cursor: params.trail[params.trail.length - 1], trail: params.trail.slice(0, -1) }
}

export function toApiParams(params: ListParams): SessionListParams {
  return {
    user_query: params.userQuery || undefined,
    user_id: params.userId || undefined,
    q: params.q || undefined,
    workspace_id: params.workspaceId || undefined,
    status: params.status || undefined,
    recording_status: params.recording || undefined,
    activity_from: params.from || undefined,
    activity_to: params.to || undefined,
    include_unrecorded: params.includeUnrecorded || undefined,
    sort: params.sort,
    cursor: params.cursor ?? undefined,
  }
}

/* ------------------------------ detail page ------------------------------ */

export interface DetailParams {
  /** Sanitised list query to return to. */
  back: string
  kinds: string[]
  statuses: string[]
  agentId: string | null
  text: string
  /** Replay position; null follows the live head. */
  at: Seq | null
  record: string | null
}

export const EMPTY_DETAIL_PARAMS: DetailParams = {
  back: "",
  kinds: [],
  statuses: [],
  agentId: null,
  text: "",
  at: null,
  record: null,
}

function list(value: string | null): string[] {
  return (value ?? "")
    .split(",")
    .map((item) => clean(item))
    .filter(Boolean)
}

export function parseDetailParams(search: URLSearchParams): DetailParams {
  const record = search.get("record")
  return {
    back: serializeListParams(parseListParams(new URLSearchParams(search.get("back") ?? ""))),
    kinds: list(search.get("kind")),
    statuses: list(search.get("status")),
    agentId: clean(search.get("agent")) || null,
    text: clean(search.get("find")),
    at: toSeq(search.get("at")),
    // Record ids embed producer ids; keep them whole rather than trimming into another id.
    record: record && record.length <= MAX_CURSOR ? record : null,
  }
}

export function serializeDetailParams(params: DetailParams): string {
  const search = new URLSearchParams()
  if (params.back) search.set("back", params.back)
  if (params.kinds.length) search.set("kind", params.kinds.join(","))
  if (params.statuses.length) search.set("status", params.statuses.join(","))
  if (params.agentId) search.set("agent", params.agentId)
  if (params.text) search.set("find", params.text)
  if (params.at) search.set("at", params.at)
  if (params.record) search.set("record", params.record)
  return search.toString()
}
