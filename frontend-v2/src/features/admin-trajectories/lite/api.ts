// Transport and query hooks of the lite view. Keys start with the same root as
// every other trajectory key, so an access purge (api/access.ts) drops these
// pages too, and every hook stops reading once access has been refused.
import { useQuery } from "@tanstack/react-query"
import { http } from "@/shared/api/http"
import { retryUnlessDenied } from "../api/access"
import { queryString, TRAJECTORY_API } from "../api/endpoints"
import { trajectoryKeys } from "../api/keys"
import { useAccessScope } from "../api/queries"
import {
  serializeTurnsParams,
  serializeUsersParams,
  windowStart,
  type TurnsParams,
  type UsersParams,
} from "./params"
import {
  EMPTY_LABELS,
  type LiteLabels,
  type LiteUserRow,
  type Page,
  type TurnDetail,
  type TurnSummary,
} from "./types"

export const USERS_PAGE_SIZE = 50
export const TURNS_PAGE_SIZE = 50
/** The "new activity" probe of a turn list while the tab is visible. */
export const TURNS_PROBE_MS = 30_000
export const LABELS_STALE_MS = 24 * 60 * 60 * 1000

const ROOT = trajectoryKeys.root[0]

export const liteKeys = {
  users: (viewer: string, params: string) => [ROOT, viewer, "lite", "users", params] as const,
  turns: (viewer: string, userId: string, params: string) =>
    [ROOT, viewer, "lite", "turns", userId, params] as const,
  turnsProbe: (viewer: string, userId: string, params: string) =>
    [ROOT, viewer, "lite", "turns-probe", userId, params] as const,
  turn: (viewer: string, sessionId: string, turnId: string) =>
    [ROOT, viewer, "target", sessionId, "lite-turn", turnId] as const,
  labels: (viewer: string) => [ROOT, viewer, "lite", "labels"] as const,
}

const withSignal = (signal?: AbortSignal): RequestInit => (signal ? { signal } : {})

export interface UsersApiParams {
  q?: string
  activity_from?: string
  activity_to?: string
  has_failures?: boolean
  active_only?: boolean
  cursor?: string
  limit?: number
}

export interface TurnsApiParams {
  status?: string[]
  activity_from?: string
  activity_to?: string
  session_id?: string
  q?: string
  cursor?: string
  limit?: number
}

function turnsQuery(params: TurnsApiParams): string {
  const { status, ...rest } = params
  const search = new URLSearchParams(queryString(rest).replace(/^\?/, ""))
  for (const value of status ?? []) search.append("status", value)
  const text = search.toString()
  return text ? `?${text}` : ""
}

export const liteApi = {
  users: (params: UsersApiParams, signal?: AbortSignal) =>
    http.get<Page<LiteUserRow>>(`${TRAJECTORY_API}/users${queryString({ ...params })}`, withSignal(signal)),
  userTurns: (userId: string, params: TurnsApiParams, signal?: AbortSignal) =>
    http.get<Page<TurnSummary>>(
      `${TRAJECTORY_API}/users/${encodeURIComponent(userId)}/turns${turnsQuery(params)}`,
      withSignal(signal),
    ),
  turn: (sessionId: string, turnId: string, signal?: AbortSignal) =>
    http.get<TurnDetail>(
      `${TRAJECTORY_API}/sessions/${encodeURIComponent(sessionId)}/turns/${encodeURIComponent(turnId)}`,
      withSignal(signal),
    ),
  labels: (signal?: AbortSignal) => http.get<LiteLabels>(`${TRAJECTORY_API}/lite/labels`, withSignal(signal)),
}

export function toUsersApiParams(params: UsersParams, limit = USERS_PAGE_SIZE): UsersApiParams {
  return {
    q: params.q || undefined,
    activity_from: windowStart(params.window),
    has_failures: params.failures || undefined,
    active_only: params.active || undefined,
    cursor: params.cursor ?? undefined,
    limit,
  }
}

export function toTurnsApiParams(params: TurnsParams, limit = TURNS_PAGE_SIZE): TurnsApiParams {
  return {
    status: params.statuses.length ? params.statuses : undefined,
    activity_from: windowStart(params.window),
    session_id: params.session || undefined,
    q: params.q || undefined,
    cursor: params.cursor ?? undefined,
    limit,
  }
}

const hidden = () => typeof document !== "undefined" && document.visibilityState === "hidden"

export function useLiteUsers(params: UsersParams) {
  const scope = useAccessScope()
  return useQuery({
    queryKey: liteKeys.users(scope.viewer, serializeUsersParams(params)),
    queryFn: ({ signal }) => liteApi.users(toUsersApiParams(params), signal),
    enabled: scope.allowed,
    staleTime: Infinity,
    retry: retryUnlessDenied,
  })
}

/** The user row of one user id, for the heading of their turn page; null until known or when unlisted. */
export function useLiteUser(userId: string) {
  const scope = useAccessScope()
  const params: UsersParams = {
    q: userId,
    window: "7d",
    failures: false,
    active: false,
    cursor: null,
    trail: [],
  }
  const query = useQuery({
    queryKey: liteKeys.users(scope.viewer, `${serializeUsersParams(params)}&limit=5`),
    queryFn: ({ signal }) => liteApi.users(toUsersApiParams(params, 5), signal),
    enabled: scope.allowed && !!userId,
    staleTime: Infinity,
    retry: retryUnlessDenied,
  })
  return { ...query, user: query.data?.items.find((row) => row.user.user_id === userId) ?? null }
}

export function useUserTurns(userId: string, params: TurnsParams) {
  const scope = useAccessScope()
  return useQuery({
    queryKey: liteKeys.turns(scope.viewer, userId, serializeTurnsParams(params)),
    queryFn: ({ signal }) => liteApi.userTurns(userId, toTurnsApiParams(params), signal),
    enabled: scope.allowed && !!userId,
    staleTime: Infinity,
    retry: retryUnlessDenied,
  })
}

/** The newest turn under these filters, re-read while the tab is visible, to say "有新活动". */
export function useUserTurnsProbe(userId: string, params: TurnsParams, enabled: boolean) {
  const scope = useAccessScope()
  const first: TurnsParams = { ...params, cursor: null, trail: [] }
  return useQuery({
    queryKey: liteKeys.turnsProbe(scope.viewer, userId, serializeTurnsParams(first)),
    queryFn: ({ signal }) => liteApi.userTurns(userId, toTurnsApiParams(first, 1), signal),
    enabled: scope.allowed && enabled && !!userId,
    refetchInterval: () => (hidden() ? false : TURNS_PROBE_MS),
    refetchIntervalInBackground: false,
    staleTime: TURNS_PROBE_MS,
    retry: retryUnlessDenied,
  })
}

export function useTurnDetail(sessionId: string, turnId: string) {
  const scope = useAccessScope()
  return useQuery({
    queryKey: liteKeys.turn(scope.viewer, sessionId, turnId),
    queryFn: ({ signal }) => liteApi.turn(sessionId, turnId, signal),
    enabled: scope.allowed && !!sessionId && !!turnId,
    staleTime: Infinity,
    retry: retryUnlessDenied,
  })
}

/** The backend dictionary; a failed read leaves raw tool ids on screen rather than an error. */
export function useLiteLabels(): LiteLabels {
  const scope = useAccessScope()
  const query = useQuery({
    queryKey: liteKeys.labels(scope.viewer),
    queryFn: ({ signal }) => liteApi.labels(signal),
    enabled: scope.allowed,
    staleTime: LABELS_STALE_MS,
    retry: retryUnlessDenied,
  })
  return query.data ?? EMPTY_LABELS
}
