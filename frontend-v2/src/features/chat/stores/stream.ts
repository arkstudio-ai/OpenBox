// Streaming message store — physically isolated from TanStack Query (§7.4).
// Pages of durable history arrive from useMessagesQuery, useLiveHistory and
// loadOlderHistory and are merged in (mergeHistory, prependHistory); every
// increment in between arrives over the WebSocket. Ported from v1's reducer,
// retyped for this project.
import { create } from "zustand"
import { setActivity } from "@/shared/lib/activity"
import type {
  MessagePart,
  MessageReaction,
  MessageWithParts,
  SessionStatus,
  ToolPart,
  ToolStatus,
} from "@/shared/types/api"

const QUIET_STATUS: ReadonlySet<SessionStatus> = new Set(["idle", "error", "waiting_input"])

type MsgMap = Map<string, MessageWithParts[]>

export interface HistoryState {
  /** Older turns exist before the oldest message held. */
  hasMore: boolean
  loadingOlder: boolean
}

interface StreamState {
  messages: MsgMap
  /** Live session status, fed by WS session.status + optimistic send. */
  status: Map<string, SessionStatus>
  /** Highest durable Driver generation observed for each session. */
  statusGeneration: Map<string, number>
  /** Generation already settled to idle/error; active frames cannot reopen it. */
  terminalStatusGeneration: Map<string, number>
  /** Which retry a stalled run is on, so the wait can account for itself. */
  retry: Map<string, { attempt: number; maxAttempts: number }>
  /** Why the last run failed, shown above the composer until the next send. */
  runError: Map<string, string>
  /** Paging state of each session's held history. */
  history: Map<string, HistoryState>
  setMessages: (sessionId: string, messages: MessageWithParts[]) => void
  /** Merge a contiguous page of durable history. A newest-turns page also says
   *  whether older turns exist. */
  mergeHistory: (sessionId: string, messages: MessageWithParts[], hasMore?: boolean) => void
  /** Put the page that precedes `before` in front of the held messages. */
  prependHistory: (sessionId: string, before: string, messages: MessageWithParts[], hasMore: boolean) => void
  setHistoryLoading: (sessionId: string, loadingOlder: boolean) => void
  addMessage: (sessionId: string, message: MessageWithParts) => void
  /** Take back an optimistic message whose send was rejected. */
  dropOptimistic: (sessionId: string, clientMessageId: string) => void
  updateMessage: (sessionId: string, message: MessageWithParts) => void
  appendPartDelta: (sessionId: string, messageId: string, partId: string, delta: string) => void
  addPart: (sessionId: string, messageId: string, part: MessagePart) => void
  updatePart: (sessionId: string, messageId: string, part: MessagePart) => void
  updateToolStatus: (
    sessionId: string,
    partId: string,
    status: ToolStatus,
    data?: Record<string, unknown>,
  ) => void
  /** Forget a session's messages so the next snapshot is taken verbatim.
   *  Needed whenever the server deleted messages: {@link setMessages} merges
   *  by keeping whichever copy has more parts, which would otherwise restore
   *  the very turn that was just removed. */
  clearMessages: (sessionId: string) => void
  setStatus: (sessionId: string, status: SessionStatus) => void
  /** Apply a server status only when its Driver generation is monotonic. */
  applyStatusEvent: (sessionId: string, status: SessionStatus, generation?: number) => boolean
  /** Reject transcript frames from an older Driver generation. */
  acceptEventGeneration: (sessionId: string, generation?: number) => boolean
  setRetry: (sessionId: string, attempt: number, maxAttempts: number) => void
  setRunError: (sessionId: string, message: string) => void
  clearRunError: (sessionId: string) => void
  /** Optimistic thumbs up/down for one message (server echo follows). */
  setMessageReaction: (sessionId: string, messageId: string, reaction: MessageReaction) => void
}

function commit(prev: MsgMap, sessionId: string, next: MessageWithParts[]): { messages: MsgMap } {
  const map = new Map(prev)
  map.set(sessionId, next)
  return { messages: map }
}

function withHistory(
  prev: Map<string, HistoryState>,
  sessionId: string,
  patch: Partial<HistoryState>,
): Map<string, HistoryState> {
  const map = new Map(prev)
  map.set(sessionId, { hasMore: false, loadingOlder: false, ...prev.get(sessionId), ...patch })
  return map
}

/** A send echoed locally before the server confirmed it. */
export function isOptimistic(message: MessageWithParts): boolean {
  return message.id.startsWith("tmp-")
}

function mapParts(
  list: MessageWithParts[],
  messageId: string,
  fn: (parts: MessagePart[]) => MessagePart[],
): MessageWithParts[] {
  return list.map((m) => (m.id === messageId ? { ...m, parts: fn(m.parts) } : m))
}

const TOOL_STATUS_RANK: Record<ToolStatus, number> = {
  pending: 0,
  running: 1,
  waiting_input: 2,
  completed: 3,
  error: 3,
}

/** Reconcile one live part with a durable snapshot.
 *
 * Text/reasoning are append-only prefixes, so the longer copy is newer. Tool
 * states are monotonic, so a delayed snapshot may fill output but may never
 * turn a completed call back into a spinner. Other parts are server-owned and
 * the freshly fetched snapshot wins (plan edits may legitimately get shorter).
 */
function mergePart(live: MessagePart, snapshot: MessagePart): MessagePart {
  if (live.type !== snapshot.type) return snapshot
  if (live.type === "text" && snapshot.type === "text") {
    return live.text.length > snapshot.text.length ? live : { ...live, ...snapshot }
  }
  if (live.type === "reasoning" && snapshot.type === "reasoning") {
    return live.text.length > snapshot.text.length ? live : { ...live, ...snapshot }
  }
  if (live.type === "tool" && snapshot.type === "tool") {
    return TOOL_STATUS_RANK[live.status] > TOOL_STATUS_RANK[snapshot.status]
      ? { ...snapshot, ...live }
      : { ...live, ...snapshot }
  }
  if (live.type === "suggestions" && snapshot.type === "suggestions") {
    // Results are terminal, including an empty result or failed generation.
    // A slower REST response must never bring the shimmer back.
    return live.status !== "pending" && snapshot.status === "pending" ? live : snapshot
  }
  return snapshot
}

/** Reconcile one held message with its durable copy, part by part. */
function reconcile(live: MessageWithParts, snapshot: MessageWithParts): MessageWithParts {
  const liveParts = new Map(live.parts.map((p) => [p.id, p]))
  const snapshotPartIds = new Set(snapshot.parts.map((p) => p.id))
  const parts = snapshot.parts.map((part) => {
    const livePart = liveParts.get(part.id)
    return livePart ? mergePart(livePart, part) : part
  })
  // A WS part may have landed after the DB SELECT but before this response.
  // Keep it; the next page will reconcile it once durable.
  parts.push(...live.parts.filter((p) => !snapshotPartIds.has(p.id)))
  const next = { ...live, ...snapshot, parts }
  // An unchanged message keeps its object, so rows built from it can skip
  // work: a live poll re-reads the newest message every second.
  return JSON.stringify(next) === JSON.stringify(live) ? live : next
}

/** Merge a contiguous page of durable history into what the view holds.
 *
 * The page covers everything between its first and last message. Held
 * messages before the first one the page shares are older pages the reader
 * already loaded, and stay as they are. Held messages after the last shared
 * one are WS frames or optimistic sends that landed after the SELECT, and stay
 * too. Between those the server is the record: a held message it no longer
 * returns was deleted — unless it is an optimistic send the server has not
 * seen yet.
 */
export function mergeSnapshotMessages(
  existing: MessageWithParts[],
  incoming: MessageWithParts[],
): MessageWithParts[] {
  if (existing.length === 0) return incoming
  if (incoming.length === 0) return existing
  const ids = new Set(incoming.map((m) => m.id))
  const cids = new Set(incoming.map((m) => m.client_message_id).filter(Boolean))
  const inPage = (m: MessageWithParts) =>
    ids.has(m.id) || Boolean(m.client_message_id && cids.has(m.client_message_id))
  const held = new Map(existing.map((m) => [m.id, m]))
  const merged = incoming.map((snapshot) => {
    const live = held.get(snapshot.id)
    return live ? reconcile(live, snapshot) : snapshot
  })
  let first = -1
  let last = -1
  existing.forEach((m, i) => {
    if (!inPage(m)) return
    if (first < 0) first = i
    last = i
  })
  if (first < 0) {
    // Nothing in common. Held messages newer than the page are WS frames that
    // landed after the SELECT, and follow it. Older ones sit beyond turns this
    // view never saw (a long disconnect, a cron session), so keeping them would
    // show a silent gap: they go, and paging back returns them in order.
    const newest = incoming[incoming.length - 1].id
    return [...merged, ...existing.filter((m) => isOptimistic(m) || m.id > newest)]
  }
  return [
    ...existing.slice(0, first),
    ...merged,
    ...existing.slice(first, last + 1).filter((m) => isOptimistic(m) && !inPage(m)),
    ...existing.slice(last + 1).filter((m) => !inPage(m)),
  ]
}

/** Narrow a loose WS tool payload into a typed patch (no `any`). */
function toolPatch(status: ToolStatus, data?: Record<string, unknown>): Partial<ToolPart> {
  const patch: Partial<ToolPart> = { status }
  if (data) {
    if (typeof data.output === "string") patch.output = data.output
    if (typeof data.error === "string") patch.error = data.error
    if (typeof data.duration === "number") patch.duration = data.duration
    if (typeof data.title === "string") patch.title = data.title
    if (data.input && typeof data.input === "object") patch.input = data.input as Record<string, unknown>
  }
  return patch
}

export const useStreamStore = create<StreamState>((set) => ({
  messages: new Map(),
  status: new Map(),
  statusGeneration: new Map(),
  terminalStatusGeneration: new Map(),
  retry: new Map(),
  runError: new Map(),
  history: new Map(),

  // Merge the durable recovery snapshot with any WS frames that landed while
  // its request was in flight. See mergeSnapshotMessages for the monotonic
  // text/tool rules that prevent a late response from moving the UI backward.
  setMessages: (sessionId, incoming) =>
    set((s) => {
      const existing = s.messages.get(sessionId) ?? []
      return commit(s.messages, sessionId, mergeSnapshotMessages(existing, incoming))
    }),

  mergeHistory: (sessionId, incoming, hasMore) =>
    set((s) => {
      const next = mergeSnapshotMessages(s.messages.get(sessionId) ?? [], incoming)
      const update = commit(s.messages, sessionId, next)
      if (hasMore === undefined) return update
      // Only a page that reaches the top of what is held can say whether older
      // turns exist; pages the reader already loaded above it answer for
      // themselves.
      const top = next.findIndex((m) => !isOptimistic(m))
      const reachesTop = incoming.length === 0 || next[top]?.id === incoming[0].id
      return reachesTop ? { ...update, history: withHistory(s.history, sessionId, { hasMore }) } : update
    }),

  prependHistory: (sessionId, before, page, hasMore) =>
    set((s) => {
      const list = s.messages.get(sessionId) ?? []
      // The view was reset while this page was in flight, so it no longer
      // belongs in front of what is held.
      if (list.find((m) => !isOptimistic(m))?.id !== before) {
        return { history: withHistory(s.history, sessionId, { loadingOlder: false }) }
      }
      const held = new Set(list.map((m) => m.id))
      return {
        ...commit(s.messages, sessionId, [...page.filter((m) => !held.has(m.id)), ...list]),
        history: withHistory(s.history, sessionId, { hasMore, loadingOlder: false }),
      }
    }),

  setHistoryLoading: (sessionId, loadingOlder) =>
    set((s) => ({ history: withHistory(s.history, sessionId, { loadingOlder }) })),

  dropOptimistic: (sessionId, clientMessageId) =>
    set((s) => {
      const list = s.messages.get(sessionId)
      if (!list) return s
      // Only ever removes the temp echo — a server-confirmed message with the
      // same client id must survive, or a slow success would erase itself.
      const next = list.filter((m) => !(m.id.startsWith("tmp-") && m.client_message_id === clientMessageId))
      if (next.length === list.length) return s
      return commit(s.messages, sessionId, next)
    }),

  addMessage: (sessionId, message) =>
    set((s) => {
      const list = s.messages.get(sessionId) ?? []
      if (list.some((m) => m.id === message.id)) return s
      // Replace the optimistic temp message once the server echo arrives.
      if (message.client_message_id) {
        const idx = list.findIndex(
          (m) => m.id.startsWith("tmp-") && m.client_message_id === message.client_message_id,
        )
        if (idx >= 0) {
          const next = [...list]
          next[idx] = message
          return commit(s.messages, sessionId, next)
        }
      }
      return commit(s.messages, sessionId, [...list, message])
    }),

  updateMessage: (sessionId, message) =>
    set((s) => {
      const list = s.messages.get(sessionId) ?? []
      return commit(
        s.messages,
        sessionId,
        list.map((m) => (m.id === message.id ? { ...m, ...message } : m)),
      )
    }),

  appendPartDelta: (sessionId, messageId, partId, delta) =>
    set((s) => {
      const list = s.messages.get(sessionId) ?? []
      return commit(
        s.messages,
        sessionId,
        mapParts(list, messageId, (parts) =>
          parts.map((p) =>
            p.id === partId && (p.type === "text" || p.type === "reasoning")
              ? { ...p, text: p.text + delta }
              : p,
          ),
        ),
      )
    }),

  addPart: (sessionId, messageId, part) =>
    set((s) => {
      const list = s.messages.get(sessionId) ?? []
      return commit(
        s.messages,
        sessionId,
        mapParts(list, messageId, (parts) =>
          parts.some((p) => p.id === part.id)
            ? parts.map((p) => p.id === part.id && part.type === "suggestions" ? mergePart(p, part) : p)
            : [...parts, part],
        ),
      )
    }),

  updatePart: (sessionId, messageId, part) =>
    set((s) => {
      const list = s.messages.get(sessionId) ?? []
      return commit(
        s.messages,
        sessionId,
        mapParts(list, messageId, (parts) => {
          // A reconnect can miss the pending event yet receive its result.
          if (part.type === "suggestions" && !parts.some((p) => p.id === part.id)) return [...parts, part]
          return parts.map((p) => p.id === part.id ? (part.type === "suggestions" ? mergePart(p, part) : part) : p)
        }),
      )
    }),

  updateToolStatus: (sessionId, partId, status, data) =>
    set((s) => {
      const list = s.messages.get(sessionId) ?? []
      const patch = toolPatch(status, data)
      return commit(
        s.messages,
        sessionId,
        list.map((m) =>
          m.parts.some((p) => p.id === partId && p.type === "tool")
            ? {
                ...m,
                parts: m.parts.map((p) => (p.id === partId && p.type === "tool" ? { ...p, ...patch } : p)),
              }
            : m,
        ),
      )
    }),

  clearMessages: (sessionId) =>
    set((s) => {
      const map = new Map(s.messages)
      map.delete(sessionId)
      const history = new Map(s.history)
      history.delete(sessionId)
      return { messages: map, history }
    }),

  setStatus: (sessionId, status) =>
    set((s) => {
      const map = new Map(s.status)
      map.set(sessionId, status)
      // A run in progress must not be cut short by app housekeeping (build swap).
      setActivity(`session:${sessionId}`, !QUIET_STATUS.has(status))
      // Leaving a stale attempt behind would have the next wait open on
      // "retry 5 of 5" before anything had gone wrong.
      const retry = new Map(s.retry)
      if (status !== "retry") retry.delete(sessionId)
      return { status: map, retry }
    }),

  applyStatusEvent: (sessionId, status, generation) => {
    let accepted = false
    set((s) => {
      const currentGeneration = s.statusGeneration.get(sessionId)
      // Generation-less frames remain compatible only until a revised frame
      // has been seen. After that point accepting a legacy terminal frame can
      // move a new run back to idle/error.
      if (
        (generation === undefined && currentGeneration !== undefined) ||
        (generation !== undefined && currentGeneration !== undefined && generation < currentGeneration)
      ) {
        return s
      }
      if (
        generation !== undefined &&
        s.terminalStatusGeneration.get(sessionId) === generation &&
        s.status.get(sessionId) !== status
      ) {
        return s
      }

      accepted = true
      setActivity(`session:${sessionId}`, !QUIET_STATUS.has(status))
      const statusMap = new Map(s.status)
      statusMap.set(sessionId, status)
      const statusGeneration = new Map(s.statusGeneration)
      if (generation !== undefined) statusGeneration.set(sessionId, generation)
      const terminalStatusGeneration = new Map(s.terminalStatusGeneration)
      if (generation !== undefined && (status === "idle" || status === "error")) {
        terminalStatusGeneration.set(sessionId, generation)
      }
      const retry = new Map(s.retry)
      if (status !== "retry") retry.delete(sessionId)
      return { status: statusMap, statusGeneration, terminalStatusGeneration, retry }
    })
    return accepted
  },

  acceptEventGeneration: (sessionId, generation) => {
    if (generation === undefined) return true
    let accepted = false
    set((s) => {
      const currentGeneration = s.statusGeneration.get(sessionId)
      if (currentGeneration !== undefined && generation < currentGeneration) return s
      accepted = true
      if (currentGeneration === generation) return s
      const statusGeneration = new Map(s.statusGeneration)
      statusGeneration.set(sessionId, generation)
      return { statusGeneration }
    })
    return accepted
  },

  setRetry: (sessionId, attempt, maxAttempts) =>
    set((s) => {
      const retry = new Map(s.retry)
      retry.set(sessionId, { attempt, maxAttempts })
      return { retry }
    }),

  setRunError: (sessionId, message) =>
    set((s) => {
      const runError = new Map(s.runError)
      runError.set(sessionId, message)
      return { runError }
    }),

  clearRunError: (sessionId) =>
    set((s) => {
      if (!s.runError.has(sessionId)) return s
      const runError = new Map(s.runError)
      runError.delete(sessionId)
      return { runError }
    }),

  setMessageReaction: (sessionId, messageId, reaction) =>
    set((s) => {
      const list = s.messages.get(sessionId) ?? []
      return commit(
        s.messages,
        sessionId,
        list.map((m) => (m.id === messageId ? { ...m, reaction } : m)),
      )
    }),
}))

const BUSY: ReadonlySet<SessionStatus> = new Set<SessionStatus>(["busy", "finalizing", "retry", "compacting"])

export function isBusyStatus(status: SessionStatus | undefined): boolean {
  return status !== undefined && BUSY.has(status)
}
