// Query keys carry the user id (ENGINEERING_SPEC §7.2).
export const skillCenterKeys = {
  all: (userId: string) => ["skill-center", userId] as const,
  skills: (userId: string) => ["skill-center", userId, "skills"] as const,
  mcp: (userId: string) => ["skill-center", userId, "mcp"] as const,
  catalog: (userId: string) => ["skill-center", userId, "catalog"] as const,
  /** Shares the workspace project-list cache without importing that feature. */
  projects: (userId: string) => ["projects", userId] as const,
  /** Shares the workspace session-list cache without importing that feature. */
  sessions: (userId: string) => ["sessions", userId] as const,
}
