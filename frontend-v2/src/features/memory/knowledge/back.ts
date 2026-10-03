import { useLocation } from "react-router"
import { paths } from "@/shared/router/paths"

/** Router state on a link into a page: the list view to return to. */
interface BackState {
  back?: string
}

/** State for a link that opens a page, so its back link returns to this exact
 *  view — same section, scope and search. Moving from page to page keeps the
 *  list the reader first came from. */
export function useBackState(): BackState {
  const location = useLocation()
  const inherited = (location.state as BackState | null)?.back
  const onPage = location.pathname.startsWith(paths.wiki() + "/")
  return { back: onPage ? inherited : location.pathname + location.search }
}

/** Where a page's back link leads: the list it was opened from, if known. */
export function useBackTarget(fallback: string): string {
  const location = useLocation()
  return (location.state as BackState | null)?.back ?? fallback
}
