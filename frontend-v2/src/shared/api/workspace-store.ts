import { create } from "zustand"

export interface WorkspaceSummary {
  id: string
  name: string
  owner_user_id: string
  kind: "personal" | "team"
  role: "owner" | "admin" | "member"
}

const STORAGE_KEY = "openbox:workspace-id"

function storedWorkspaceId(): string | null {
  if (typeof window === "undefined") return null
  return window.localStorage.getItem(STORAGE_KEY)
}

interface WorkspaceState {
  items: WorkspaceSummary[]
  currentId: string | null
  setItems: (items: WorkspaceSummary[], defaultId: string | null) => void
  setCurrent: (workspaceId: string) => void
  clear: () => void
}

export const useWorkspaceStore = create<WorkspaceState>((set) => ({
  items: [],
  currentId: storedWorkspaceId(),
  setItems: (items, defaultId) =>
    set((state) => {
      const persisted = state.currentId ?? storedWorkspaceId()
      const currentId = items.some((item) => item.id === persisted)
        ? persisted
        : (defaultId ?? items[0]?.id ?? null)
      if (typeof window !== "undefined") {
        if (currentId) window.localStorage.setItem(STORAGE_KEY, currentId)
        else window.localStorage.removeItem(STORAGE_KEY)
      }
      return { items, currentId }
    }),
  setCurrent: (currentId) => {
    if (typeof window !== "undefined") window.localStorage.setItem(STORAGE_KEY, currentId)
    set({ currentId })
  },
  clear: () => {
    if (typeof window !== "undefined") window.localStorage.removeItem(STORAGE_KEY)
    set({ items: [], currentId: null })
  },
}))
