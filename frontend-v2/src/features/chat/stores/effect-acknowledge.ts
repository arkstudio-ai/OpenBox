import { create } from "zustand"

/** Tasks whose resume this tab saw refused for unverified outcomes
 *  (ASSISTANT_EFFECT_UNRESOLVED): their cards offer "我已核对，继续" even
 *  before the task view itself reports effect_unknown. */
export const useEffectAcknowledgeRequests = create<{
  taskIds: ReadonlySet<string>
  request: (taskId: string) => void
  clear: (taskId: string) => void
}>((set) => ({
  taskIds: new Set(),
  request: (taskId) => set((state) => ({ taskIds: new Set(state.taskIds).add(taskId) })),
  clear: (taskId) => set((state) => {
    const taskIds = new Set(state.taskIds)
    taskIds.delete(taskId)
    return { taskIds }
  }),
}))

export function requestEffectAcknowledge(taskId: string) {
  useEffectAcknowledgeRequests.getState().request(taskId)
}
