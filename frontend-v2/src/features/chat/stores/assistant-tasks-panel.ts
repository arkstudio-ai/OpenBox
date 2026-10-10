import { create } from "zustand"

/** The top bar and compact conversation reminder open the same task drawer. */
export const useAssistantTasksPanel = create<{ open: boolean; setOpen: (open: boolean) => void }>((set) => ({
  open: false,
  setOpen: (open) => set({ open }),
}))
