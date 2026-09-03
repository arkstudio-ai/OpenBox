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
  cron: "/app/cron",
  skills: "/app/skills",
  resources: (projectId?: string) =>
    projectId ? `/app/resources?project=${projectId}` : "/app/resources",
} as const

export const routePatterns = {
  invite: "/invite/:token",
  chat: "s/:sessionId",
  settings: "settings/:tab?",
  cron: "cron",
  skills: "skills",
  resources: "resources",
} as const
