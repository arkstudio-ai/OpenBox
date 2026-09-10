// Workspace shell UI state: sidebar width/collapse, which project rows are
// expanded, and which project is the current working one (new chats land in
// it). Pure client state (ENGINEERING_SPEC §7.1). Expansion and selection are
// persisted: losing them to a reload or a "new chat" reads as the sidebar
// forgetting where the user was working.
import { create } from "zustand"

export type SessionFilter = "chats" | "cron"

interface WorkspaceUiState {
  sidebarWidth: number
  sidebarCollapsed: boolean
  mobileSidebarOpen: boolean
  expanded: Record<string, boolean>
  selectedProject: string | null
  // The conversation most recently open, so a centre page can hand the
  // person back to it. Persisted: a reload on a centre page still knows.
  lastSessionId: string | null
  // Per-project sidebar filter: plain conversations (default) or cron runs.
  // Deliberately not persisted — a fresh load always starts on conversations.
  sessionFilter: Record<string, SessionFilter>
  setSidebarWidth: (w: number) => void
  toggleSidebar: () => void
  toggleMobileSidebar: () => void
  closeMobileSidebar: () => void
  toggleProject: (id: string) => void
  isExpanded: (id: string) => boolean
  selectProject: (id: string | null) => void
  setLastSession: (id: string) => void
  setSessionFilter: (projectId: string, mode: SessionFilter) => void
}

const KEY = "bossip:workspace-ui"

interface Persisted {
  sidebarWidth?: number
  sidebarCollapsed?: boolean
  expanded?: Record<string, boolean>
  selectedProject?: string | null
  lastSessionId?: string | null
}

function readLocal(): Persisted {
  try {
    return JSON.parse(localStorage.getItem(KEY) ?? "{}") as Persisted
  } catch {
    return {}
  }
}

export const useWorkspaceUi = create<WorkspaceUiState>((set, get) => {
  const local = readLocal()
  const persist = () => {
    const s = get()
    localStorage.setItem(
      KEY,
      JSON.stringify({
        sidebarWidth: s.sidebarWidth,
        sidebarCollapsed: s.sidebarCollapsed,
        expanded: s.expanded,
        selectedProject: s.selectedProject,
        lastSessionId: s.lastSessionId,
      }),
    )
  }
  return {
    sidebarWidth: Math.min(420, Math.max(220, local.sidebarWidth ?? 280)),
    sidebarCollapsed: local.sidebarCollapsed ?? false,
    mobileSidebarOpen: false,
    toggleMobileSidebar: () => set((s) => ({ mobileSidebarOpen: !s.mobileSidebarOpen })),
    closeMobileSidebar: () => set({ mobileSidebarOpen: false }),
    expanded: local.expanded ?? {},
    selectedProject: local.selectedProject ?? null,
    lastSessionId: local.lastSessionId ?? null,
    sessionFilter: {},
    setSidebarWidth: (w) => {
      set({ sidebarWidth: Math.min(420, Math.max(220, w)), sidebarCollapsed: false })
      persist()
    },
    toggleSidebar: () => {
      set((s) => ({ sidebarCollapsed: !s.sidebarCollapsed }))
      persist()
    },
    toggleProject: (id) => {
      set((s) => ({ expanded: { ...s.expanded, [id]: !(s.expanded[id] ?? true) } }))
      persist()
    },
    isExpanded: (id) => get().expanded[id] ?? true,
    selectProject: (id) => {
      set({ selectedProject: id })
      persist()
    },
    setLastSession: (id) => {
      if (get().lastSessionId === id) return
      set({ lastSessionId: id })
      persist()
    },
    setSessionFilter: (projectId, mode) => {
      set((s) => ({ sessionFilter: { ...s.sessionFilter, [projectId]: mode } }))
    },
  }
})
