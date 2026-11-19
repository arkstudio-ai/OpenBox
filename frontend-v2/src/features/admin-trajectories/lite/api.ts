// Transport and query hooks of the lite view. Keys start with the same root as
// every other trajectory key, so an access purge (api/access.ts) drops these
// pages too, and every hook stops reading once access has been refused.
import { useInfiniteQuery, useQuery } from "@tanstack/react-query"
import { http } from "@/shared/api/http"
import { retryUnlessDenied } from "../api/access"
import { queryString, TRAJECTORY_API, trajectoryApi } from "../api/endpoints"
import { trajectoryKeys } from "../api/keys"
import { useAccessScope } from "../api/queries"
import type { Seq } from "../types/protocol"
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
  type SessionReplay,
  type TurnDetail,
  type TurnSummary,
} from "./types"

export const USERS_PAGE_SIZE = 50
export const TURNS_PAGE_SIZE = 50
/** The "new activity" probe of a turn list while the tab is visible. */
export const TURNS_PROBE_MS = 30_000
/** Turns per replay page; the server allows up to 30. */
export const REPLAY_PAGE_SIZE = 10
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
  replay: (viewer: string, sessionId: string) => [ROOT, viewer, "target", sessionId, "lite-replay"] as const,
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
  order?: "asc"
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
  replay: (sessionId: string, cursor: string | null, signal?: AbortSignal) =>
    http.get<SessionReplay>(
      `${TRAJECTORY_API}/sessions/${encodeURIComponent(sessionId)}/replay${queryString({
        cursor: cursor ?? undefined,
        limit: REPLAY_PAGE_SIZE,
      })}`,
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
    order: params.order === "asc" ? "asc" : undefined,
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
  // Always newest first, whatever the list shows: the probe's one row must be the newest turn.
  const first: TurnsParams = { ...params, order: "desc", cursor: null, trail: [] }
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

/** A session as the person saw it, oldest turn first; each further page continues downwards. */
export function useSessionReplay(sessionId: string) {
  const scope = useAccessScope()
  return useInfiniteQuery({
    queryKey: liteKeys.replay(scope.viewer, sessionId),
    queryFn: ({ pageParam, signal }) => liteApi.replay(sessionId, pageParam, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => (last.has_more ? last.next_cursor : undefined),
    enabled: scope.allowed && !!sessionId,
    staleTime: Infinity,
    retry: retryUnlessDenied,
  })
}

/** How long a signed media link is shown before a remount asks for a new one; the server signs for ten minutes. */
export const MEDIA_LINK_STALE_MS = 8 * 60 * 1000

/**
 * A short-lived signed link to a file of the conversation (an image the person attached, a video the
 * assistant produced), asked for only once the file is on screen. The server audits and re-checks every
 * link; a refusal (deleted, not linkable, not yet produced) leaves the file shown by name.
 */
export function useMediaLink(sessionId: string, payloadId: string | null, throughSeq: Seq, shown: boolean) {
  const scope = useAccessScope()
  return useQuery({
    queryKey: [...liteKeys.replay(scope.viewer, sessionId), "media-link", payloadId ?? ""] as const,
    queryFn: ({ signal }) => trajectoryApi.payloadLink(sessionId, payloadId ?? "", throughSeq, { signal }),
    enabled: scope.allowed && shown && !!payloadId,
    staleTime: MEDIA_LINK_STALE_MS,
    gcTime: MEDIA_LINK_STALE_MS,
    // A new link would swap the source under a playing video.
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    retry: false,
  })
}
