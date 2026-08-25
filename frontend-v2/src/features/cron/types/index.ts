// Wire types for /api/cron/* (backend cron/service.py _job_to_dict/_run_to_dict).

export interface CronScheduleAt {
  kind: "at"
  at: string
}

export interface CronScheduleEvery {
  kind: "every"
  every_ms: number
  anchor_ms?: number | null
}

export interface CronScheduleCron {
  kind: "cron"
  expr: string
  tz?: string
}

export type CronSchedule = CronScheduleAt | CronScheduleEvery | CronScheduleCron

export interface CronJob {
  id: string
  user_id: string
  /** Owning project — the task runs in its directory. */
  project_id: string | null
  /** Optional conversation results are posted into (chat-created jobs). */
  session_id: string | null
  name: string
  description: string
  enabled: boolean
  schedule: CronSchedule
  task_prompt: string
  agent: string
  model: string | null
  timeout_seconds: number
  delivery: Record<string, unknown>
  delete_after_run: boolean
  next_run_at: string | null
  last_run_at: string | null
  last_status: string | null
  last_error: string | null
  last_duration_ms: number | null
  consecutive_errors: number
  total_runs: number
  total_successes: number
  total_failures: number
  running: boolean
  created_at: string | null
  updated_at: string | null
  project_directory?: string | null
}

export interface CronProjectOption {
  id: string
  name: string
}

export interface CronRun {
  id: string
  job_id: string
  temp_session_id: string | null
  status: "ok" | "error" | "skipped" | "running"
  error_message: string | null
  task_prompt: string | null
  summary_text: string | null
  injected: boolean
  input_tokens: number
  output_tokens: number
  total_tokens: number
  duration_ms: number
  started_at: string | null
  ended_at: string | null
}

export interface CronStatus {
  running: boolean
  healthy: boolean
  last_tick_at: string | null
  next_run_at: string | null
  total_jobs: number
  enabled_jobs: number
  running_jobs: number
}

export interface CronJobCreateInput {
  project_id: string
  session_id?: string | null
  name: string
  schedule: CronSchedule
  task_prompt: string
}

export interface CronJobUpdateInput {
  name?: string
  task_prompt?: string
  schedule?: CronSchedule
  enabled?: boolean
}
