import { useSyncExternalStore } from "react"
import { useWorkspaceUi } from "../stores/ui"

const QUERY = "(max-width: 767px)"
function compact() {
  return typeof window.matchMedia === "function" && window.matchMedia(QUERY).matches
}
function subscribe(listener: () => void) {
  if (typeof window.matchMedia !== "function") return () => undefined
  const media = window.matchMedia(QUERY)
  media.addEventListener("change", listener)
  return () => media.removeEventListener("change", listener)
}

/** Mobile navigation never changes the user's persisted desktop preference. */
export function useSidebarLayout() {
  const isCompact = useSyncExternalStore(subscribe, compact, () => false)
  const collapsed = useWorkspaceUi((s) => s.sidebarCollapsed)
  const mobileOpen = useWorkspaceUi((s) => s.mobileSidebarOpen)
  const toggleDesktop = useWorkspaceUi((s) => s.toggleSidebar)
  const toggleMobile = useWorkspaceUi((s) => s.toggleMobileSidebar)
  const closeMobile = useWorkspaceUi((s) => s.closeMobileSidebar)
  return {
    compact: isCompact,
    open: isCompact ? mobileOpen : !collapsed,
    toggle: isCompact ? toggleMobile : toggleDesktop,
    close: isCompact ? closeMobile : toggleDesktop,
  }
}
