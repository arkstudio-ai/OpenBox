export const jobKeys = {
  all: (userId: string) => ["skill-jobs", userId] as const,
  session: (userId: string, sessionId: string) =>
    ["skill-jobs", userId, "session", sessionId] as const,
  detail: (userId: string, jobId: string) => ["skill-jobs", userId, "detail", jobId] as const,
  artifacts: (userId: string, jobId: string) =>
    ["skill-jobs", userId, "artifacts", jobId] as const,
}
