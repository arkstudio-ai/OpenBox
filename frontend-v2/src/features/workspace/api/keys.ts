// Query key factory — every key carries the user id so switching accounts
// can never serve another user's cache (ENGINEERING_SPEC §7.2).
export const workspaceKeys = {
  projects: (userId: string, workspaceId: string | null) => ["projects", userId, workspaceId] as const,
  sessions: (userId: string, workspaceId: string | null) => ["sessions", userId, workspaceId] as const,
  session: (userId: string, workspaceId: string | null, id: string) => ["session", userId, workspaceId, id] as const,
}
