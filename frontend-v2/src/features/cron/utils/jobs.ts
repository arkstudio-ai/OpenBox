// Pure helpers over cron rows, shared by the list page, the task page and the
// sidebar so the three agree on what a job's state looks like.
import type { CronJob, CronRun } from "@/features/cron/types"

/** The scheduler switched the job off after repeated failures. */
export const isAutoDisabled = (job: CronJob): boolean => (job.last_error ?? "").startsWith("[auto-disabled")

/** Colour of the state dot: live, armed, tripped, or off. */
export function jobDotClass(job: CronJob): string {
  if (job.running) return "bg-accent animate-pulse"
  if (job.enabled) return "bg-sage"
  return isAutoDisabled(job) ? "bg-danger" : "bg-n400"
}

/** Which run the task page opens on: the one the URL names when it still
 *  exists, else the newest run that left a transcript, else the newest run. */
export function pickRun(runs: readonly CronRun[], requested: string | null): CronRun | null {
  if (requested) {
    const hit = runs.find((run) => run.id === requested)
    if (hit) return hit
  }
  return runs.find((run) => run.temp_session_id) ?? runs[0] ?? null
}
