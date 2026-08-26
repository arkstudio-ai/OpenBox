// Resource-centre pane sizing. Pure client UI state (ENGINEERING_SPEC §7.1),
// persisted like the sidebar's width: a column someone dragged to fit their
// filenames should still be that wide after a reload.
import { create } from "zustand"

const KEY = "bossip:resources-ui"

/** Narrow enough to still show a name, wide enough not to eat the preview. */
export const LIST_MIN_WIDTH = 220
export const LIST_MAX_WIDTH = 560
const LIST_DEFAULT_WIDTH = 288 // matches the previous fixed w-72

interface ResourcesUiState {
  listWidth: number
  setListWidth: (width: number) => void
}

function clamp(width: number): number {
  return Math.min(LIST_MAX_WIDTH, Math.max(LIST_MIN_WIDTH, Math.round(width)))
}

function readLocal(): number {
  try {
    const raw = JSON.parse(localStorage.getItem(KEY) ?? "{}") as { listWidth?: number }
    return clamp(raw.listWidth ?? LIST_DEFAULT_WIDTH)
  } catch {
    return LIST_DEFAULT_WIDTH
  }
}

export const useResourcesUi = create<ResourcesUiState>((set, get) => ({
  listWidth: readLocal(),
  setListWidth: (width) => {
    set({ listWidth: clamp(width) })
    localStorage.setItem(KEY, JSON.stringify({ listWidth: get().listWidth }))
  },
}))
