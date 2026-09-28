// Query keys carry the user id (ENGINEERING_SPEC §7.2). Accounts are per
// workspace, which the http layer selects via header, so the workspace id is
// part of the key too or a switch would show the previous workspace's rows.
export const authCenterKeys = {
  all: (userId: string) => ["auth-center", userId] as const,
  platforms: (userId: string) => ["auth-center", userId, "platforms"] as const,
  accounts: (userId: string, workspaceId: string) =>
    ["auth-center", userId, "accounts", workspaceId] as const,
  job: (userId: string, jobId: string) => ["auth-center", userId, "job", jobId] as const,
  videos: (userId: string, workspaceId: string) =>
    ["auth-center", userId, "videos", workspaceId] as const,
}
