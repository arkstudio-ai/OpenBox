import { useState } from "react"
import { useTranslation } from "react-i18next"
import { cn } from "@/shared/lib/cn"
import { formatRelative } from "@/shared/lib/format"
import { toast } from "@/shared/ui/Toast"
import { useApiErrorMessage } from "@/shared/hooks/useApiErrorMessage"
import {
  useAnswerSkillJob,
  useCancelSkillJob,
  useSkillJobArtifacts,
} from "@/features/jobs/api/jobs"
import { TERMINAL_JOB_STATUSES, type SkillJobSnapshot } from "@/features/jobs/types"
import { AssetPreview } from "@/shared/ui/AssetPreview"

/** Status → dot/tone. Exported for tests. */
export function statusTone(status: SkillJobSnapshot["status"]): {
  dot: string
  labelKey: string
  pulse: boolean
} {
  switch (status) {
    case "running":
      return { dot: "bg-accent", labelKey: "status.running", pulse: true }
    case "queued":
      return { dot: "bg-n400", labelKey: "status.queued", pulse: false }
    case "retry_scheduled":
      return { dot: "bg-n400", labelKey: "status.retry_scheduled", pulse: false }
    case "waiting_external":
      return { dot: "bg-accent", labelKey: "status.waiting_external", pulse: false }
    case "waiting_user":
      return { dot: "bg-accent", labelKey: "status.waiting_user", pulse: true }
    case "waiting_agent":
      return { dot: "bg-accent", labelKey: "status.waiting_agent", pulse: false }
    case "succeeded":
      return { dot: "bg-sage", labelKey: "status.succeeded", pulse: false }
    case "failed":
      return { dot: "bg-danger", labelKey: "status.failed", pulse: false }
    case "cancelled":
      return { dot: "bg-n400", labelKey: "status.cancelled", pulse: false }
    default:
      // A rolling deploy can surface a status this bundle does not know yet;
      // degrade instead of crashing the card render.
      return { dot: "bg-n400", labelKey: `status.${status}`, pulse: false }
  }
}

export function skillDisplayName(skillKey: string): string {
  return skillKey.replace(/^(builtin|user):/, "")
}

// waiting_user bookkeeping lives in progress_data for the snapshot's sake;
// the AnswerForm renders it, so the generic line must not repeat it.
const HIDDEN_PROGRESS_KEYS = new Set(["prompt", "input_schema", "expires_at"])

function ProgressLine({ progress }: { progress: Record<string, unknown> }) {
  const entries = Object.entries(progress).filter(
    ([k, v]) => v !== null && v !== undefined && !HIDDEN_PROGRESS_KEYS.has(k),
  )
  if (entries.length === 0) return null
  return (
    <div className="truncate text-xs text-n500">
      {entries.map(([k, v]) => `${k}: ${typeof v === "object" ? JSON.stringify(v) : String(v)}`).join(" · ")}
    </div>
  )
}

/** One-line summary of a succeeded job's result. Exported for tests. */
/** Identifiers are addresses for machines, not information for a reader.
 *
 *  A finished video job returns asset_id, segment_id, video_job_id and
 *  production_id — printing them made the card read like a debug dump while
 *  the thing the person actually wanted, the video, sat right underneath.
 */
function isIdentifier(key: string): boolean {
  return key === "id" || key.endsWith("_id") || key.endsWith("Id")
}

export function resultSummary(result: Record<string, unknown>): string | null {
  const entries = Object.entries(result).filter(
    ([k, v]) => v !== null && v !== undefined && !isIdentifier(k),
  )
  if (entries.length === 0) return null
  const text = entries
    .map(([k, v]) => `${k}: ${typeof v === "object" ? JSON.stringify(v) : String(v)}`)
    .join(" · ")
  return text.length > 200 ? `${text.slice(0, 200)}…` : text
}

function AnswerForm({ job }: { job: SkillJobSnapshot }) {
  const { t } = useTranslation("jobs")
  const [text, setText] = useState("")
  const answer = useAnswerSkillJob()
  const errorMessage = useApiErrorMessage()
  const prompt = typeof job.progress["prompt"] === "string" ? (job.progress["prompt"] as string) : null
  // An empty input_schema marks a prompt-only park (e.g. operator review):
  // the handler will not consume free text, so offering a box would be a lie.
  const schema = job.progress["input_schema"]
  const acceptsInput =
    !!schema && typeof schema === "object" && Object.keys(schema as object).length > 0

  const submit = () => {
    const value = text.trim()
    if (!value || answer.isPending) return
    answer.mutate(
      {
        jobId: job.jobId,
        payload: { text: value },
        // Keyed to the prompt round: double-clicks and reconnect replays
        // cannot consume the answer twice (skill_job_inputs unique key).
        idempotencyKey: `ui:${job.jobId}:${job.lastEventSeq}`,
      },
      {
        onSuccess: () => setText(""),
        onError: (err) => toast("error", errorMessage(err)),
      },
    )
  }

  return (
    <div className="mt-2 space-y-1.5">
      {prompt && <div className="text-sm text-n700">{prompt}</div>}
      {acceptsInput && (
      <div className="flex gap-1.5">
        <input
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") submit()
          }}
          placeholder={t("card.answerPlaceholder")}
          className="min-w-0 flex-1 rounded-md border border-n200 bg-surface px-2 py-1 text-sm outline-none focus:border-accent"
        />
        <button
          type="button"
          onClick={submit}
          disabled={answer.isPending || !text.trim()}
          className="bg-ink text-bg rounded-md px-2.5 py-1 text-sm disabled:opacity-50"
        >
          {t("card.answerSend")}
        </button>
      </div>
      )}
    </div>
  )
}

export function SkillJobCard({ job }: { job: SkillJobSnapshot }) {
  const { t } = useTranslation("jobs")
  const tone = statusTone(job.status)
  const terminal = TERMINAL_JOB_STATUSES.has(job.status)
  const cancel = useCancelSkillJob()
  const errorMessage = useApiErrorMessage()
  const artifacts = useSkillJobArtifacts(job.jobId, job.status === "succeeded")

  const phaseLabel = job.phase
    ? t(job.phaseLabelKey ?? "", { defaultValue: job.phase })
    : null

  return (
    <div className="rounded-xl border border-n200 bg-surface p-3">
      <div className="flex items-center gap-2">
        <span className={cn("size-2 shrink-0 rounded-full", tone.dot, tone.pulse && "animate-pulse")} />
        <span className="truncate text-sm font-medium text-n800">
          {skillDisplayName(job.skillKey)} · {job.operation}
        </span>
        <span className="ms-auto shrink-0 text-xs text-n500">{t(tone.labelKey)}</span>
      </div>

      {/* Phase and progress describe work in flight; a settled card shows its
          outcome instead of the last waypoint. */}
      {!terminal && phaseLabel && <div className="mt-1 text-xs text-n600">{phaseLabel}</div>}
      {!terminal && <ProgressLine progress={job.progress} />}

      {job.status === "succeeded" && resultSummary(job.result) && (
        <div className="mt-1 break-words text-xs text-n600">{resultSummary(job.result)}</div>
      )}
      {job.errorMessage && (
        <div className="mt-1 break-words text-xs text-danger">{job.errorMessage}</div>
      )}

      {job.status === "waiting_user" && <AnswerForm job={job} />}

      {(artifacts.data ?? []).map((a) => (
        <AssetPreview key={a.artifactId} artifact={a} />
      ))}

      <div className="mt-2 flex items-center gap-2 text-xs text-n500">
        {job.updatedAt && <span>{formatRelative(job.updatedAt)}</span>}
        {!terminal && job.desiredState !== "cancel" && (
          <button
            type="button"
            className="ms-auto text-n500 hover:text-danger"
            onClick={() =>
              cancel.mutate(job.jobId, {
                onError: (err) => toast("error", errorMessage(err)),
              })
            }
            disabled={cancel.isPending}
          >
            {t("card.cancel")}
          </button>
        )}
        {!terminal && job.desiredState === "cancel" && (
          <span className="ms-auto">{t("card.cancelling")}</span>
        )}
      </div>
    </div>
  )
}
