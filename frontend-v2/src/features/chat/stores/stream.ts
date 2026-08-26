// Streaming message store — physically isolated from TanStack Query (§7.4).
// The initial snapshot comes from useMessagesQuery (setMessages); every later
// increment arrives over the WebSocket. Server state is never re-poured in
// beyond that first merge. Ported from v1's reducer, retyped for this project.
import { create } from "zustand"
import type {
  MessagePart,
  MessageReaction,
  MessageWithParts,
  SessionStatus,
  ToolPart,
  ToolStatus,
} from "@/shared/types/api"

type MsgMap = Map<string, MessageWithParts[]>

interface StreamState {
  messages: MsgMap
  /** Live session status, fed by WS session.status + optimistic send. */
  status: Map<string, SessionStatus>
  setMessages: (sessionId: string, messages: MessageWithParts[]) => void
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
  /** Optimistic thumbs up/down for one message (server echo follows). */
  setMessageReaction: (sessionId: string, messageId: string, reaction: MessageReaction) => void
}

function commit(prev: MsgMap, sessionId: string, next: MessageWithParts[]): { messages: MsgMap } {
  const map = new Map(prev)
  map.set(sessionId, next)
  return { messages: map }
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
  completed: 2,
  error: 2,
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
  return snapshot
}

export function mergeSnapshotMessages(
  existing: MessageWithParts[],
  incoming: MessageWithParts[],
): MessageWithParts[] {
  if (existing.length === 0) return incoming
  const incomingIds = new Set(incoming.map((m) => m.id))
  const incomingCids = new Set(incoming.map((m) => m.client_message_id).filter(Boolean))
  const merged = incoming.map((snapshot) => {
    const live = existing.find((m) => m.id === snapshot.id)
    if (!live) return snapshot
    const liveParts = new Map(live.parts.map((p) => [p.id, p]))
    const snapshotPartIds = new Set(snapshot.parts.map((p) => p.id))
    const parts = snapshot.parts.map((part) => {
      const livePart = liveParts.get(part.id)
      return livePart ? mergePart(livePart, part) : part
    })
    // A WS part may have landed after the DB SELECT but before this response.
    // Keep it; the next snapshot will reconcile it once durable.
    parts.push(...live.parts.filter((p) => !snapshotPartIds.has(p.id)))
    return { ...live, ...snapshot, parts }
  })
  const extras = existing.filter(
    (m) => !incomingIds.has(m.id) && !(m.client_message_id && incomingCids.has(m.client_message_id)),
  )
  return [...merged, ...extras]
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

  // Merge the durable recovery snapshot with any WS frames that landed while
  // its request was in flight. See mergeSnapshotMessages for the monotonic
  // text/tool rules that prevent a late response from moving the UI backward.
  setMessages: (sessionId, incoming) =>
    set((s) => {
      const existing = s.messages.get(sessionId) ?? []
      return commit(s.messages, sessionId, mergeSnapshotMessages(existing, incoming))
    }),

  dropOptimistic: (sessionId, clientMessageId) =>
    set((s) => {
      const list = s.messages.get(sessionId)
      if (!list) return s
      // Only ever removes the temp echo — a server-confirmed message with the
      // same client id must survive, or a slow success would erase itself.
      const next = list.filter(
        (m) => !(m.id.startsWith("tmp-") && m.client_message_id === clientMessageId),
      )
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
          parts.some((p) => p.id === part.id) ? parts : [...parts, part],
        ),
      )
    }),

  updatePart: (sessionId, messageId, part) =>
    set((s) => {
      const list = s.messages.get(sessionId) ?? []
      return commit(
        s.messages,
        sessionId,
        mapParts(list, messageId, (parts) => parts.map((p) => (p.id === part.id ? part : p))),
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
      return { messages: map }
    }),

  setStatus: (sessionId, status) =>
    set((s) => {
      const map = new Map(s.status)
      map.set(sessionId, status)
      return { status: map }
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
