// Pending permission & question requests, keyed by session. Seeded from the
// list queries on mount / reconnect, kept live by the WS bridge.
import { create } from "zustand"
import { useAuthStore } from "@/shared/api/auth-store"
import type { PermissionRequest, QuestionRequest } from "@/shared/types/api"

type PermMap = Map<string, PermissionRequest[]>
type QsMap = Map<string, QuestionRequest[]>

export interface QuestionRead {
  sequence: number
  revision: number
  epoch: number
}

let readSequence = 0

interface PendingState {
  permissions: PermMap
  questions: QsMap
  closedQuestions: Set<string>
  questionRevision: number
  questionVersions: Map<string, number>
  questionEpoch: number
  latestQuestionRead: number
  setPermissions: (items: PermissionRequest[]) => void
  addPermission: (item: PermissionRequest) => void
  removePermission: (requestId: string) => void
  beginQuestionRead: () => QuestionRead
  setQuestions: (items: QuestionRequest[], read?: QuestionRead) => void
  reset: () => void
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

function boundClosed(closed: Set<string>): Set<string> {
  while (closed.size > 1000) closed.delete(closed.values().next().value!)
  return closed
}

export const usePendingStore = create<PendingState>((set, get) => ({
  permissions: new Map(),
  questions: new Map(),
  closedQuestions: new Set(),
  questionRevision: 0,
  questionVersions: new Map(),
  questionEpoch: 0,
  latestQuestionRead: 0,
  reset: () =>
    set((s) => ({
      permissions: new Map(),
      questions: new Map(),
      closedQuestions: new Set(),
      questionRevision: 0,
      questionVersions: new Map(),
      latestQuestionRead: 0,
      questionEpoch: s.questionEpoch + 1,
    })),
  setPermissions: (items) => set({ permissions: groupBySession(items) }),
  addPermission: (item) => set((s) => ({ permissions: add(s.permissions, item) })),
  removePermission: (requestId) => set((s) => ({ permissions: remove(s.permissions, requestId) })),
  beginQuestionRead: () => ({
    sequence: ++readSequence,
    revision: get().questionRevision,
    epoch: get().questionEpoch,
  }),
  setQuestions: (items, read) =>
    set((s) => {
      if (read && (read.epoch !== s.questionEpoch || read.sequence < s.latestQuestionRead)) return s
      const merged = new Map(
        items
          .filter((q) => !s.closedQuestions.has(q.id) && (!q.status || q.status === "pending"))
          .map((q) => [q.id, q]),
      )
      const closed = new Set(s.closedQuestions)
      for (const list of s.questions.values()) {
        for (const live of list) {
          const saved = merged.get(live.id)
          const arrivedDuringRead = read && (s.questionVersions.get(live.id) ?? 0) > read.revision
          if (
            (saved && (live.draft_revision ?? 0) > (saved.draft_revision ?? 0)) ||
            (!saved && arrivedDuringRead && !closed.has(live.id))
          ) {
            merged.set(live.id, live)
          } else if (!saved) {
            // A fresh authoritative absence is terminal; a delayed asked event
            // must not revive a question answered while this client was offline.
            closed.add(live.id)
          }
        }
      }
      return {
        questions: groupBySession([...merged.values()]),
        closedQuestions: boundClosed(closed),
        questionVersions: new Map([...s.questionVersions].filter(([id]) => merged.has(id))),
        latestQuestionRead: read?.sequence ?? s.latestQuestionRead,
      }
    }),
  addQuestion: (item) =>
    set((s) => {
      if (s.closedQuestions.has(item.id) || (item.status && item.status !== "pending")) return s
      const questions = new Map(s.questions)
      const list = questions.get(item.session_id) ?? []
      const existing = list.find((q) => q.id === item.id)
      if (existing && (existing.draft_revision ?? 0) >= (item.draft_revision ?? 0)) return s
      questions.set(
        item.session_id,
        existing ? list.map((q) => (q.id === item.id ? item : q)) : [...list, item],
      )
      const questionRevision = s.questionRevision + 1
      const questionVersions = new Map(s.questionVersions).set(item.id, questionRevision)
      return { questions, questionRevision, questionVersions }
    }),
  removeQuestion: (requestId) =>
    set((s) => {
      const closedQuestions = new Set(s.closedQuestions)
      closedQuestions.add(requestId)
      const questionVersions = new Map(s.questionVersions)
      questionVersions.delete(requestId)
      return {
        questions: remove(s.questions, requestId),
        closedQuestions: boundClosed(closedQuestions),
        questionRevision: s.questionRevision + 1,
        questionVersions,
      }
    }),
}))

useAuthStore.subscribe((current, previous) => {
  if (current.user?.id !== previous.user?.id) usePendingStore.getState().reset()
})
