// URL state of the lite pages. Like the professional list (utils/params.ts),
// filters, the page cursor and the cursors behind it live in the URL so a
// detail page can send the operator back to exactly the page they left.
import { opaqueCursor } from "../utils/params"
import { ABNORMAL_STATUSES, isTurnStatus, type TurnStatus } from "./types"

export const USER_WINDOWS = ["today", "7d", "30d", "all"] as const
export type UserWindow = (typeof USER_WINDOWS)[number]
export const DEFAULT_USER_WINDOW: UserWindow = "7d"

export const TURN_WINDOWS = ["7d", "30d", "90d", "all"] as const
export type TurnWindow = (typeof TURN_WINDOWS)[number]
export const DEFAULT_TURN_WINDOW: TurnWindow = "30d"

interface Paged {
  cursor: string | null
  /** Cursors of the pages before this one; `null` is the first page. */
  trail: ReadonlyArray<string | null>
}

export interface UsersParams extends Paged {
  q: string
  window: UserWindow
  failures: boolean
  active: boolean
}

export interface TurnsParams extends Paged {
  statuses: TurnStatus[]
  session: string
  q: string
  window: TurnWindow
}

export const EMPTY_USERS_PARAMS: UsersParams = {
  q: "",
  window: DEFAULT_USER_WINDOW,
  failures: false,
  active: false,
  cursor: null,
  trail: [],
}

export const EMPTY_TURNS_PARAMS: TurnsParams = {
  statuses: [],
  session: "",
  q: "",
  window: DEFAULT_TURN_WINDOW,
  cursor: null,
  trail: [],
}

const FIRST_PAGE = "."
const MAX_TEXT = 200

function clean(value: string | null): string {
  return (value ?? "").trim().slice(0, MAX_TEXT)
}

function parseTrail(value: string | null): Array<string | null> | null {
  if (!value) return []
  const trail: Array<string | null> = []
  for (const item of value.split(",")) {
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

function parsePaging(search: URLSearchParams): Paged {
  const rawCursor = search.get("cursor")
  const cursor = opaqueCursor(rawCursor)
  const trail = parseTrail(search.get("trail"))
  // A damaged cursor or trail cannot be repaired; restart paging on the same filters.
  if ((rawCursor && cursor === null) || trail === null) return { cursor: null, trail: [] }
  return { cursor, trail }
}

function serializePaging(search: URLSearchParams, params: Paged): void {
  if (params.cursor) search.set("cursor", params.cursor)
  if (params.trail.length) search.set("trail", params.trail.map((item) => item ?? FIRST_PAGE).join(","))
}

export function parseUsersParams(search: URLSearchParams): UsersParams {
  const window = search.get("window")
  return {
    q: clean(search.get("q")),
    window: (USER_WINDOWS as readonly string[]).includes(window ?? "")
      ? (window as UserWindow)
      : DEFAULT_USER_WINDOW,
    failures: search.get("failures") === "1",
    active: search.get("active") === "1",
    ...parsePaging(search),
  }
}

/** Canonical query string (no leading "?"); defaults are omitted. */
export function serializeUsersParams(params: UsersParams): string {
  const search = new URLSearchParams()
  if (params.q) search.set("q", params.q)
  if (params.window !== DEFAULT_USER_WINDOW) search.set("window", params.window)
  if (params.failures) search.set("failures", "1")
  if (params.active) search.set("active", "1")
  serializePaging(search, params)
  return search.toString()
}

export function parseTurnsParams(search: URLSearchParams): TurnsParams {
  const window = search.get("window")
  const statuses = (search.get("status") ?? "")
    .split(",")
    .map((item) => item.trim())
    .filter(isTurnStatus)
  return {
    statuses: [...new Set(statuses)],
    session: clean(search.get("session")),
    q: clean(search.get("q")),
    window: (TURN_WINDOWS as readonly string[]).includes(window ?? "")
      ? (window as TurnWindow)
      : DEFAULT_TURN_WINDOW,
    ...parsePaging(search),
  }
}

export function serializeTurnsParams(params: TurnsParams): string {
  const search = new URLSearchParams()
  if (params.statuses.length) search.set("status", params.statuses.join(","))
  if (params.session) search.set("session", params.session)
  if (params.q) search.set("q", params.q)
  if (params.window !== DEFAULT_TURN_WINDOW) search.set("window", params.window)
  serializePaging(search, params)
  return search.toString()
}

/** Filters changed: the old cursors belong to another list, so paging restarts. */
export function withFilters<P extends Paged>(params: P, patch: Partial<P>): P {
  return { ...params, ...patch, cursor: null, trail: [] }
}

export function nextPage<P extends Paged>(params: P, nextCursor: string): P {
  return { ...params, cursor: nextCursor, trail: [...params.trail, params.cursor] }
}

export function previousPage<P extends Paged>(params: P): P {
  if (!params.trail.length) return params
  return {
    ...params,
    cursor: params.trail[params.trail.length - 1] ?? null,
    trail: params.trail.slice(0, -1),
  }
}

/** Whether the "只看异常" switch is on: exactly the abnormal statuses are selected. */
export function isAbnormalOnly(statuses: readonly TurnStatus[]): boolean {
  return (
    statuses.length === ABNORMAL_STATUSES.length &&
    ABNORMAL_STATUSES.every((status) => statuses.includes(status))
  )
}

/**
 * Start of the window as an ISO instant, in the operator's local time: "today"
 * is midnight today, "7d" midnight six days ago, so the statistics columns are
 * stable within a day and agree with what "近 7 天" means to a person.
 */
export function windowStart(window: UserWindow | TurnWindow, now: Date = new Date()): string | undefined {
  if (window === "all") return undefined
  const days = window === "today" ? 1 : Number.parseInt(window, 10)
  const start = new Date(now.getFullYear(), now.getMonth(), now.getDate())
  start.setDate(start.getDate() - (days - 1))
  return start.toISOString()
}

const LEGACY_LIST_KEYS = ["owner", "user", "workspace", "status", "recording", "unrecorded", "sort"]

/**
 * A query string written by the professional session list before it moved to
 * `/sessions`: it carries filter keys the lite user list never writes. `q`,
 * `from`, `to`, `cursor` and `trail` alone are not enough to tell the two apart.
 */
export function isLegacyListSearch(search: URLSearchParams): boolean {
  return LEGACY_LIST_KEYS.some((key) => search.has(key))
}
