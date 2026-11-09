// Whether a task card is folded to one line. A display preference, not a
// server fact: it lives in this browser only (localStorage, best effort), so
// "不再跟进" stays the one server-side action and nothing syncs across devices.
//
// Default: a finished card (done / failed / stopped / idle) folds itself once
// it is no longer fresh — the result of a round just completed is shown in
// full once; from the next round on it takes one line. Active and waiting
// cards stay open. A manual fold or unfold wins over the default.
import { create } from "zustand"
import { isActiveTask, type TaskStatus } from "../lib/task-status"

const KEY = "openbox.taskCardFold"
/** A finished task updated within this long is "just done" and stays open by default. */
export const FRESH_MS = 10 * 60_000
const LIMIT = 400

type Fold = "folded" | "open"
interface FoldStore {
  overrides: Record<string, Fold>
  finishedGroupOpen: boolean
  setFold: (taskId: string, folded: boolean) => void
  setFinishedGroupOpen: (open: boolean) => void
}

function load(): Pick<FoldStore, "overrides" | "finishedGroupOpen"> {
  try {
    const raw = typeof localStorage === "undefined" ? null : localStorage.getItem(KEY)
    const data = raw ? (JSON.parse(raw) as Partial<Pick<FoldStore, "overrides" | "finishedGroupOpen">>) : null
    return { overrides: data?.overrides && typeof data.overrides === "object" ? data.overrides : {},
      finishedGroupOpen: data?.finishedGroupOpen === true }
  } catch { return { overrides: {}, finishedGroupOpen: false } }
}

function save(state: Pick<FoldStore, "overrides" | "finishedGroupOpen">): void {
  try {
    // Keep the newest entries only; a stale override for a long-gone task is harmless.
    const entries = Object.entries(state.overrides).slice(-LIMIT)
    localStorage.setItem(KEY, JSON.stringify({ overrides: Object.fromEntries(entries), finishedGroupOpen: state.finishedGroupOpen }))
  } catch { /* private window or full storage: the default rule still applies */ }
}

export const useTaskCardFold = create<FoldStore>((set, get) => ({
  ...load(),
  setFold: (taskId, folded) => {
    const overrides = { ...get().overrides, [taskId]: folded ? "folded" as const : "open" as const }
    set({ overrides })
    save({ overrides, finishedGroupOpen: get().finishedGroupOpen })
  },
  setFinishedGroupOpen: (open) => {
    set({ finishedGroupOpen: open })
    save({ overrides: get().overrides, finishedGroupOpen: open })
  },
}))

/** The default before any manual choice: finished and not fresh → folded. */
export function defaultFolded(status: TaskStatus, updatedAt: string | null | undefined, now = Date.now()): boolean {
  if (isActiveTask(status)) return false
  const updated = updatedAt ? Date.parse(updatedAt) : NaN
  if (Number.isFinite(updated) && now - updated < FRESH_MS) return false
  return true
}

export function isFolded(taskId: string, status: TaskStatus, updatedAt: string | null | undefined, overrides: Record<string, Fold>): boolean {
  const override = overrides[taskId]
  if (override) return override === "folded"
  return defaultFolded(status, updatedAt)
}
