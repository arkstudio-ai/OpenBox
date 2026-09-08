// Route path constants (ENGINEERING_SPEC §8.2) — never hardcode paths.
export const paths = {
  landing: "/",
  login: "/login",
  register: "/register",
  ssoCallback: "/callback",
  invite: (token: string) => `/invite/${encodeURIComponent(token)}`,
  app: "/app",
  chat: (sessionId: string) => `/app/s/${sessionId}`,
  settings: (tab?: string) => (tab ? `/app/settings/${tab}` : "/app/settings"),
  billing: (tab?: string) => (tab ? `/app/billing/${tab}` : "/app/billing"),
  cron: "/app/cron",
  skills: "/app/skills",
  authCenter: "/app/auth-center",
  resources: (projectId?: string) =>
    projectId ? `/app/resources?project=${projectId}` : "/app/resources",
  admin: "/app/admin",
  // Kept at its original value: links to the fleet page predate the console
  // shell and are still handed around in ops runbooks.
  adminFleet: "/app/admin/fleet",
  adminSkills: (tab?: string) => (tab ? `/app/admin/skills/${tab}` : "/app/admin/skills"),
  adminBilling: (tab?: string) => (tab ? `/app/admin/billing/${tab}` : "/app/admin/billing"),
  adminWorkspace: (workspaceId: string) =>
    `/app/admin/billing/workspaces/${encodeURIComponent(workspaceId)}`,
} as const

export const routePatterns = {
  invite: "/invite/:token",
  chat: "s/:sessionId",
  settings: "settings/:tab?",
  billing: "billing/:tab?",
  cron: "cron",
  skills: "skills",
  authCenter: "auth-center",
  resources: "resources",
  // The console shell owns `/app/admin`; its columns are relative children of
  // that route, so what used to be an `/app` child is now just "fleet".
  admin: "admin",
  adminFleet: "fleet",
  adminSkills: "skills/:tab?",
  adminBilling: "billing/:tab?",
  adminWorkspace: "billing/workspaces/:workspaceId",
} as const
