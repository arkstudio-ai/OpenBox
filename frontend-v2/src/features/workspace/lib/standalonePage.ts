import { matchPath } from "react-router"
import { paths } from "@/shared/router/paths"

/** Pages under /app that are not a conversation. Everything else is the chat
 *  surface — a session or the new-chat greeting. */
export type StandalonePage =
  | "settings"
  | "billing"
  | "cron"
  | "resources"
  | "authCenter"
  | "skills"
  | "admin"

// Route patterns, not substring checks: "/app/s/<id>" must never read as a
// settings page because the id happens to contain "settings".
const PAGES: ReadonlyArray<readonly [StandalonePage, string]> = [
  ["settings", `${paths.settings()}/*`],
  ["billing", `${paths.billing()}/*`],
  ["cron", paths.cron],
  ["resources", paths.resources()],
  ["authCenter", paths.authCenter],
  ["skills", paths.skills],
  ["admin", `${paths.admin}/*`],
]

/** Which standalone page a pathname is on, or null for the chat surface. */
export function standalonePage(pathname: string): StandalonePage | null {
  for (const [page, pattern] of PAGES) {
    if (matchPath(pattern, pathname)) return page
  }
  return null
}
