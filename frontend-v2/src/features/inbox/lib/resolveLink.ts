// Allow-listed inbox link → navigation (docs/MESSAGE_CENTER.md §link). The
// same rules as the phone's InboxNavigator: confirm the user still belongs to
// the target workspace (and can still see the session), switch scope, then
// route. Unknown kinds land in the inbox; nothing here opens a URL the server
// did not vet.
import { useCallback } from "react"
import { useNavigate } from "react-router"
import { http } from "@/shared/api/http"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import { paths } from "@/shared/router/paths"
import type { InboxLink } from "../api"

export type LinkPlan =
  | { action: "navigate"; to: string; workspaceId?: string; sessionId?: string }
  | { action: "external"; url: string }
  | { action: "unavailable" }

const SLUG = /^[a-z0-9][a-z0-9-]{1,63}$/

const PANEL_QUERY = "panel=desktop"

function planUrl(url: string): LinkPlan {
  try {
    return new URL(url).protocol === "https:" ? { action: "external", url } : { action: "unavailable" }
  } catch {
    return { action: "unavailable" }
  }
}

function planSession(link: InboxLink, workspaceId: string): LinkPlan {
  const sessionId = "sessionId" in link ? link.sessionId : ""
  if (!sessionId) return { action: "unavailable" }
  const takeover = "panel" in link && link.panel === "desktop"
  const control = "control" in link && Boolean(link.control)
  const to = !takeover
    ? paths.chat(sessionId)
    : control
      ? paths.desktopTakeover(sessionId)
      : `${paths.chat(sessionId)}?${PANEL_QUERY}`
  return { action: "navigate", to, workspaceId, sessionId }
}

function planInWorkspace(link: InboxLink, memberOf: readonly string[]): LinkPlan {
  const workspaceId = "workspaceId" in link ? link.workspaceId : ""
  if (!workspaceId || !memberOf.includes(workspaceId)) return { action: "unavailable" }
  switch (link.kind) {
    case "session":
      return planSession(link, workspaceId)
    case "cron":
      return { action: "navigate", to: paths.cron, workspaceId }
    case "skills":
      return { action: "navigate", to: paths.skills, workspaceId }
    default: {
      const jobId = "jobId" in link ? link.jobId : undefined
      const to = jobId ? `${paths.authCenter}?job=${encodeURIComponent(jobId)}` : paths.authCenter
      return { action: "navigate", to, workspaceId }
    }
  }
}

/** Pure planning step; `memberOf` is the workspace ids the user can use. */
export function planInboxLink(link: InboxLink | null, memberOf: readonly string[]): LinkPlan {
  if (!link) return { action: "navigate", to: paths.inbox }
  switch (link.kind) {
    case "url":
      return planUrl("url" in link ? link.url : "")
    case "topic": {
      const slug = "slug" in link ? link.slug : ""
      return SLUG.test(slug) ? { action: "navigate", to: paths.topic(slug) } : { action: "unavailable" }
    }
    case "admin_skills":
      return { action: "navigate", to: paths.adminSkills() }
    case "session":
    case "cron":
    case "auth_center":
    case "skills":
      return planInWorkspace(link, memberOf)
    default:
      return { action: "navigate", to: paths.inbox }
  }
}

export type OpenResult = "opened" | "unavailable"

/** Runs a plan: session access check, workspace switch, then navigation. */
export function useOpenInboxLink() {
  const navigate = useNavigate()
  return useCallback(
    async (link: InboxLink | null): Promise<OpenResult> => {
      const store = useWorkspaceStore.getState()
      const plan = planInboxLink(
        link,
        store.items.map((item) => item.id),
      )
      if (plan.action === "unavailable") return "unavailable"
      if (plan.action === "external") {
        window.open(plan.url, "_blank", "noopener,noreferrer")
        return "opened"
      }
      if (plan.sessionId && plan.workspaceId) {
        try {
          await http.get(`/api/agent/session/${encodeURIComponent(plan.sessionId)}`, {
            headers: { "X-Workspace-Id": plan.workspaceId },
          })
        } catch {
          return "unavailable"
        }
      }
      if (plan.workspaceId && plan.workspaceId !== store.currentId) store.setCurrent(plan.workspaceId)
      void navigate(plan.to)
      return "opened"
    },
    [navigate],
  )
}
