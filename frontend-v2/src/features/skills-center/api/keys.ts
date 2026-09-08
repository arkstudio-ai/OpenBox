// Query keys carry the user id (ENGINEERING_SPEC §7.2).
export const skillCenterKeys = {
  all: (userId: string) => ["skill-center", userId] as const,
  skills: (userId: string) => ["skill-center", userId, "skills"] as const,
  mcp: (userId: string) => ["skill-center", userId, "mcp"] as const,
  catalog: (userId: string) => ["skill-center", userId, "catalog"] as const,
  /** Deliberately the settings feature's key: /api/agent/config is one
   *  request whose answer is identical for both readers, and a second cache
   *  entry would mean a second round trip on every visit to the centre. */
  config: (userId: string) => ["agent-config", userId] as const,
  /** Shares the workspace project-list cache without importing that feature. */
  projects: (userId: string) => ["projects", userId] as const,
  /** Shares the workspace session-list cache without importing that feature. */
  sessions: (userId: string) => ["sessions", userId] as const,
}
