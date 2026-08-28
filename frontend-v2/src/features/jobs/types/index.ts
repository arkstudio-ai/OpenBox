// Snapshot shapes served by /api/skill-jobs (backend skill_runtime/service.py
// job_snapshot). The snapshot GET is authoritative; WS events only invalidate.

export type SkillJobStatus =
  | "queued"
  | "running"
  | "waiting_external"
  | "waiting_user"
  | "waiting_agent"
  | "retry_scheduled"
  | "succeeded"
  | "failed"
  | "cancelled"

export const TERMINAL_JOB_STATUSES: ReadonlySet<SkillJobStatus> = new Set([
  "succeeded",
  "failed",
  "cancelled",
])

export interface SkillJobSnapshot {
  jobId: string
  skillKey: string
  operation: string
  status: SkillJobStatus
  phase: string | null
  phaseLabelKey: string | null
  desiredState: "run" | "cancel"
  progress: Record<string, unknown>
  result: Record<string, unknown>
  errorCode: string | null
  errorMessage: string | null
  attempt: number
  maxAttempts: number
  sessionId: string | null
  queue: string
  lastEventSeq: number
  nextRunAt: string | null
  deadlineAt: string | null
  createdAt: string | null
  updatedAt: string | null
  completedAt: string | null
}

export interface SkillJobEventRow {
  seq: number
  eventType: string
  payload: Record<string, unknown>
  createdAt: string | null
}

export interface SkillJobArtifact {
  artifactId: string
  assetId: string
  role: string
  ordinal: number
  name: string
  mime: string
  size: number
  status: string
  metadata: Record<string, unknown>
}
