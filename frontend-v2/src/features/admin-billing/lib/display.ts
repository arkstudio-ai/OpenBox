// Small display helpers shared by the three pages.
import { formatDateTime } from "@/shared/lib/format"

/** An em dash, so an empty cell still occupies a line and reads as "no value". */
export const DASH = "—"

/**
 * Half of the fields in this feature are nullable (a workspace that never paid,
 * an order that was never cancelled), and every one of them would otherwise
 * render "Invalid Date".
 */
export function formatWhen(iso: string | null | undefined): string {
  if (!iso) return DASH
  const at = new Date(iso)
  return Number.isNaN(at.getTime()) ? DASH : formatDateTime(iso)
}
