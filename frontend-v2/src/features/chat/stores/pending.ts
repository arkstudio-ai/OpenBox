// Pending permission & question requests, keyed by session. Seeded from the
// list queries on mount / reconnect, kept live by the WS bridge.
import { create } from "zustand"
import type { PermissionRequest, QuestionRequest } from "@/shared/types/api"

type PermMap = Map<string, PermissionRequest[]>
type QsMap = Map<string, QuestionRequest[]>

interface PendingState {
  permissions: PermMap
  questions: QsMap
  closedQuestions: Set<string>
  setPermissions: (items: PermissionRequest[]) => void
  addPermission: (item: PermissionRequest) => void
  removePermission: (requestId: string) => void
  setQuestions: (items: QuestionRequest[]) => void
  addQuestion: (item: QuestionRequest) => void
  removeQuestion: (requestId: string) => void
}

function groupBySession<T extends { session_id: string }>(items: T[]): Map<string, T[]> {
  const map = new Map<string, T[]>()
  for (const item of items) {
    const list = map.get(item.session_id) ?? []
    list.push(item)
    map.set(item.session_id, list)
  }
  return map
}

function add<T extends { id: string; session_id: string }>(
  prev: Map<string, T[]>,
  item: T,
): Map<string, T[]> {
  const map = new Map(prev)
  const list = map.get(item.session_id) ?? []
  if (list.some((x) => x.id === item.id)) return prev
  map.set(item.session_id, [...list, item])
  return map
}

function remove<T extends { id: string }>(prev: Map<string, T[]>, requestId: string): Map<string, T[]> {
  const map = new Map<string, T[]>()
  let changed = false
  for (const [sid, list] of prev) {
    const next = list.filter((x) => x.id !== requestId)
    if (next.length !== list.length) changed = true
    map.set(sid, next)
  }
  return changed ? map : prev
}

export const usePendingStore = create<PendingState>((set) => ({
  permissions: new Map(),
  questions: new Map(),
  closedQuestions: new Set(),
  setPermissions: (items) => set({ permissions: groupBySession(items) }),
  addPermission: (item) => set((s) => ({ permissions: add(s.permissions, item) })),
  removePermission: (requestId) => set((s) => ({ permissions: remove(s.permissions, requestId) })),
  setQuestions: (items) => set((s) => ({ questions: groupBySession(items.filter((q) =>
    !s.closedQuestions.has(q.id) && (!q.status || q.status === "pending")).map((q) => {
    const live = s.questions.get(q.session_id)?.find((item) => item.id === q.id)
    return live && (live.draft_revision ?? 0) > (q.draft_revision ?? 0) ? live : q
  })) })),
  addQuestion: (item) => set((s) => {
    if (s.closedQuestions.has(item.id) || (item.status && item.status !== "pending")) return s
    const questions = new Map(s.questions)
    const list = questions.get(item.session_id) ?? []
    const existing = list.find((q) => q.id === item.id)
    if (existing && (existing.draft_revision ?? 0) >= (item.draft_revision ?? 0)) return s
    questions.set(item.session_id, existing ? list.map((q) => q.id === item.id ? item : q) : [...list, item])
    return { questions }
  }),
  removeQuestion: (requestId) => set((s) => {
    const closedQuestions = new Set(s.closedQuestions)
    closedQuestions.add(requestId)
    // Bound transport tombstones; server snapshots remain authoritative.
    if (closedQuestions.size > 1000) closedQuestions.delete(closedQuestions.values().next().value!)
    return { questions: remove(s.questions, requestId), closedQuestions }
  }),
}))
