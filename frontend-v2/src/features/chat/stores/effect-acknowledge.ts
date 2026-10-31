import { create } from "zustand"

/** Tasks whose resume this tab saw refused for unverified outcomes
 *  (ASSISTANT_EFFECT_UNRESOLVED): their cards offer "我已核对，继续" even
 *  before the task view itself reports effect_unknown. Execution pages are
 *  tracked by session: a send refused there shows the same control above the
 *  composer, so the person is never pointed at a card on another page. */
export const useEffectAcknowledgeRequests = create<{
  taskIds: ReadonlySet<string>
  sessionIds: ReadonlySet<string>
  request: (taskId: string) => void
  clear: (taskId: string) => void
  requestSession: (sessionId: string) => void
  clearSession: (sessionId: string) => void
}>((set) => ({
  taskIds: new Set(),
  sessionIds: new Set(),
  request: (taskId) => set((state) => ({ taskIds: new Set(state.taskIds).add(taskId) })),
  clear: (taskId) => set((state) => {
    const taskIds = new Set(state.taskIds)
    taskIds.delete(taskId)
    return { taskIds }
  }),
  requestSession: (sessionId) => set((state) => ({ sessionIds: new Set(state.sessionIds).add(sessionId) })),
  clearSession: (sessionId) => set((state) => {
    const sessionIds = new Set(state.sessionIds)
    sessionIds.delete(sessionId)
    return { sessionIds }
  }),
}))

export function requestEffectAcknowledge(taskId: string) {
  useEffectAcknowledgeRequests.getState().request(taskId)
}

export function requestEffectAcknowledgeForSession(sessionId: string) {
  useEffectAcknowledgeRequests.getState().requestSession(sessionId)
}
