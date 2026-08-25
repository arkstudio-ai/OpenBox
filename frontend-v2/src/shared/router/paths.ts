// Route path constants (ENGINEERING_SPEC §8.2) — never hardcode paths.
export const paths = {
  landing: "/",
  login: "/login",
  register: "/register",
  ssoCallback: "/callback",
  app: "/app",
  chat: (sessionId: string) => `/app/s/${sessionId}`,
  settings: (tab?: string) => (tab ? `/app/settings/${tab}` : "/app/settings"),
  cron: "/app/cron",
} as const

export const routePatterns = {
  chat: "s/:sessionId",
  settings: "settings/:tab?",
  cron: "cron",
} as const
