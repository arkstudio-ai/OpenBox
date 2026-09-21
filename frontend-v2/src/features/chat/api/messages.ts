import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useAuthStore } from "@/shared/api/auth-store"
import { ApiError, http } from "@/shared/api/http"
import type { MessageWithParts, Session, TeamRequest } from "@/shared/types/api"
import { chatKeys } from "./keys"
import { usePendingStore } from "../stores/pending"
import { isOptimistic, useStreamStore } from "../stores/stream"

export function useUserId(): string {
  return useAuthStore((s) => s.user?.id ?? "anonymous")
}

/** Turns per history page. A turn runs from one user message to the next, so a
 *  run of tool steps never straddles two pages. */
export const HISTORY_TURNS = 8

export interface HistoryPage {
  messages: MessageWithParts[]
  /** Older messages exist before the first one returned (never after a cursor). */
  has_more: boolean
}

export interface HistoryCursor {
  /** The turns before this message. */
  before?: string
  /** This message and everything newer: the part of a chat that can still change. */
  after?: string
  turns?: number
}

/** A page of a conversation, newest turns first. Chats used to be read from
 *  offset 0 to the end on every open and every poll — a megabyte a second for a
 *  350-message conversation while a run was live. */
export function fetchHistory(sessionId: string, cursor: HistoryCursor = {}, signal?: AbortSignal): Promise<HistoryPage> {
  const params = new URLSearchParams({ turns: String(cursor.turns ?? HISTORY_TURNS) })
  if (cursor.before) params.set("before", cursor.before)
  if (cursor.after) params.set("after", cursor.after)
  return http.get<HistoryPage>(`/api/agent/session/${sessionId}/history?${params}`, { signal })
}

/** The message a page was anchored to was deleted (regenerate, dismiss,
 *  revert): drop what is held and start again from the newest turns. */
export function isHistoryCursorGone(error: unknown): boolean {
  return error instanceof ApiError && error.code === "HISTORY_CURSOR_GONE"
}

/** Where live catch-up resumes: the newest message the server has confirmed. */
export function newestPersistedId(messages: readonly MessageWithParts[] | undefined): string | undefined {
  const list = messages ?? []
  for (let i = list.length - 1; i >= 0; i -= 1) {
    if (!isOptimistic(list[i])) return list[i].id
  }
  return undefined
}

/** The newest turns of a conversation. Taken on every route mount and again
 *  whenever an event invalidates it — a terminal status, an answered question,
 *  a reconnect — then merged into any older pages the reader has loaded. */
export function useMessagesQuery(sessionId: string) {
  const userId = useUserId()
  return useQuery({
    queryKey: chatKeys.messages(userId, sessionId),
    queryFn: ({ signal }) => fetchHistory(sessionId, {}, signal),
    enabled: sessionId.length > 0,
    refetchOnMount: "always",
  })
}

export interface LiveHistory extends HistoryPage {
  /** Nothing confirmed was held yet, so this is the newest window, not a catch-up. */
  windowed: boolean
}

/** While a run is live, re-read the newest confirmed message and everything
 *  after it once a second. A reconnect replays only durable state, not the
 *  frames missed while the page was away, so this is how a reopened page
 *  converges — without downloading the rest of the conversation each time. */
export function useLiveHistory(sessionId: string, live: boolean) {
  const userId = useUserId()
  return useQuery({
    queryKey: chatKeys.liveHistory(userId, sessionId),
    queryFn: async ({ signal }): Promise<LiveHistory> => {
      const after = newestPersistedId(useStreamStore.getState().messages.get(sessionId))
      const page = await fetchHistory(sessionId, after ? { after } : {}, signal)
      return { ...page, windowed: !after }
    },
    enabled: live && sessionId.length > 0,
    refetchInterval: live ? 1_000 : false,
    // The next tick is the retry; a vanished anchor is handled by the caller.
    retry: false,
    gcTime: 0,
  })
}

/** Put the turns before the oldest confirmed message in front of the view. */
export async function loadOlderHistory(sessionId: string): Promise<void> {
  const store = useStreamStore.getState()
  const before = store.messages.get(sessionId)?.find((m) => !isOptimistic(m))?.id
  if (!before || store.history.get(sessionId)?.loadingOlder) return
  store.setHistoryLoading(sessionId, true)
  try {
    const page = await fetchHistory(sessionId, { before })
    useStreamStore.getState().prependHistory(sessionId, before, page.messages, page.has_more)
  } finally {
    useStreamStore.getState().setHistoryLoading(sessionId, false)
  }
}

export interface SendMessageVars {
  teamRequest?: TeamRequest
  text: string
  model?: string
  /** null explicitly clears the conversation override. */
  variant?: string | null
  /** Video model for this turn; the backend records it on the session. */
  videoModel?: string
  /** Video resolution tier for this turn; recorded on the session like the model. */
  videoResolution?: string
  agent?: string
  attachments?: string[]
  clientMessageId: string
}

/** One wire path for both a new conversation's first prompt and later turns. */
export function sendPromptAsync(sessionId: string, vars: SendMessageVars) {
  return http.post<{ ok: boolean }>(`/api/agent/session/${sessionId}/prompt_async`, {
    text: vars.text,
    agent: vars.agent,
    team_request: vars.teamRequest,
    model: vars.model,
    video_model: vars.videoModel,
    video_resolution: vars.videoResolution,
    variant: vars.variant,
    attachments: vars.attachments?.length ? vars.attachments : undefined,
    client_message_id: vars.clientMessageId,
  })
}

export function useSendMessage(sessionId: string) {
  const qc = useQueryClient()
  const userId = useUserId()
  return useMutation({
    mutationFn: (vars: SendMessageVars) => sendPromptAsync(sessionId, vars),
    // The backend records the chosen model on the session, so the cached copy
    // is stale the moment a send goes out — and it is what restores the picker
    // when the user comes back to this conversation.
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["session", userId, sessionId] }),
  })
}

export function useAbortSession(sessionId: string) {
  const qc = useQueryClient()
  const userId = useUserId()
  return useMutation({
    mutationFn: () => http.post<{ ok: boolean }>(`/api/agent/session/${sessionId}/abort`),
    onMutate: () => (usePendingStore.getState().questions.get(sessionId) ?? []).map((q) => q.id),
    onSuccess: (_result, _vars, oldQuestions) => {
      for (const id of oldQuestions ?? []) usePendingStore.getState().removeQuestion(id)
      void qc.invalidateQueries({ queryKey: ["session", userId, sessionId] })
      void qc.invalidateQueries({ queryKey: chatKeys.questions(userId) })
      void qc.invalidateQueries({ queryKey: chatKeys.messages(userId, sessionId) })
    },
  })
}

export interface CreateSessionVars {
  projectId?: string
  model?: string
  variant?: string | null
  agent?: string
}

export function useCreateSession() {
  const userId = useUserId()
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (vars: CreateSessionVars) =>
      http.post<Session>("/api/agent/session", {
        project_id: vars.projectId,
        model: vars.model,
        variant: vars.variant,
        agent: vars.agent,
      }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["sessions", userId] }),
  })
}
