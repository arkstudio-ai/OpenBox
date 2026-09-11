// Route path constants (ENGINEERING_SPEC §8.2) — never hardcode paths.
export const paths = {
  landing: "/",
  login: "/login",
  register: "/register",
  ssoCallback: "/callback",
  invite: (token: string) => `/invite/${encodeURIComponent(token)}`,
  app: "/app",
  // The greeting + composer is the workspace home; a fresh chat starts there.
  // With a project it files the first message under that project.
  newChat: (projectId?: string) => (projectId ? `/app?project=${projectId}` : "/app"),
  chat: (sessionId: string) => `/app/s/${sessionId}`,
  /** The chat, with the cloud desktop panel opened and input control on —
   *  what a takeover card links to. A real URL so it survives a reload and
   *  can be handed to a phone or a notification later. */
  desktopTakeover: (sessionId: string) => `/app/s/${sessionId}?${PANEL_PARAM}=desktop&${CONTROL_PARAM}=1`,
  settings: (tab?: string) => (tab ? `/app/settings/${tab}` : "/app/settings"),
  billing: (tab?: string) => (tab ? `/app/billing/${tab}` : "/app/billing"),
  cron: "/app/cron",
  skills: "/app/skills",
  authCenter: "/app/auth-center",
  inbox: "/app/inbox",
  /** Public topic page behind a first-party notice; the route lands with M2. */
  topic: (slug: string) => `/topics/${encodeURIComponent(slug)}`,
  resources: (projectId?: string) => (projectId ? `/app/resources?project=${projectId}` : "/app/resources"),
  admin: "/app/admin",
  // Kept at its original value: links to the fleet page predate the console
  // shell and are still handed around in ops runbooks.
  adminFleet: "/app/admin/fleet",
  adminNotifications: "/app/admin/notifications",
  adminMessages: (tab?: string) => (tab ? `/app/admin/messages/${tab}` : "/app/admin/messages"),
  adminSkills: (tab?: string) => (tab ? `/app/admin/skills/${tab}` : "/app/admin/skills"),
  adminBilling: (tab?: string) => (tab ? `/app/admin/billing/${tab}` : "/app/admin/billing"),
  adminWorkspace: (workspaceId: string) => `/app/admin/billing/workspaces/${encodeURIComponent(workspaceId)}`,
} as const

/** Query params a chat URL may carry to open a workbench panel on arrival. */
export const PANEL_PARAM = "panel"
export const CONTROL_PARAM = "control"

export interface PanelRequest {
  kind: "desktop"
  control: boolean
}

/** Read `?panel=desktop&control=1` off a chat URL; null when there is none. */
export function readPanelRequest(params: URLSearchParams): PanelRequest | null {
  if (params.get(PANEL_PARAM) !== "desktop") return null
  return { kind: "desktop", control: params.get(CONTROL_PARAM) === "1" }
}

export const routePatterns = {
  invite: "/invite/:token",
  chat: "s/:sessionId",
  settings: "settings/:tab?",
  billing: "billing/:tab?",
  cron: "cron",
  skills: "skills",
  authCenter: "auth-center",
  inbox: "inbox",
  topic: "/topics/:slug",
  resources: "resources",
  // The console shell owns `/app/admin`; its columns are relative children of
  // that route, so what used to be an `/app` child is now just "fleet".
  admin: "admin",
  adminFleet: "fleet",
  adminNotifications: "notifications",
  adminMessages: "messages/:tab?",
  adminSkills: "skills/:tab?",
  adminBilling: "billing/:tab?",
  adminWorkspace: "billing/workspaces/:workspaceId",
} as const
