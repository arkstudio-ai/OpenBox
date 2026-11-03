// Whether the four-step "how we work together" tour is open (VOICE_SEAM_PLAN §12.6). The assistant
// page opens it once for a person who has not seen it; the topbar's "怎么用" opens it any time.
import { create } from "zustand"

interface TourStore {
  open: boolean
  setOpen: (open: boolean) => void
}

export const useAssistantTour = create<TourStore>((set) => ({ open: false, setOpen: (open) => set({ open }) }))

export function openAssistantTour(): void {
  useAssistantTour.getState().setOpen(true)
}
