// Query keys carry the user id (ENGINEERING_SPEC §7.2).
export const cronKeys = {
  jobs: (userId: string) => ["cron-jobs", userId] as const,
  status: (userId: string) => ["cron-status", userId] as const,
  runs: (userId: string, jobId: string) => ["cron-runs", userId, jobId] as const,
}
