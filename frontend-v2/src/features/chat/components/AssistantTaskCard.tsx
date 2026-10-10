// A task the personal assistant follows, as one plain card: what it is, where
// it runs, whether it needs you, and the latest word from it. Controls (pause,
// resume, cancel, stop following) sit behind "更多"; ids and receipts never
// reach the page.
import { useRef, useState } from "react"
import { Link } from "react-router"
import { ArrowUpRight, MoreHorizontal } from "lucide-react"
import { useTranslation } from "react-i18next"
import type { MessagePart, ToolPart } from "@/shared/types/api"
import { paths } from "@/shared/router/paths"
import { ApiError } from "@/shared/api/http"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import { useApiErrorMessage } from "@/shared/hooks/useApiErrorMessage"
import { cn } from "@/shared/lib/cn"
import { Menu, MenuItem } from "@/shared/ui/Menu"
import { StatusPill } from "@/shared/ui/StatusPill"
import { toast } from "@/shared/ui/Toast"
import {
  useAssistantArchive, useAssistantControl, useAssistantResult, useAssistantTask, useRetryAssistantReport,
  type AssistantControlAction, type AssistantResult, type AssistantTaskView,
} from "../api/assistant"
import { useAssistantWatch, type AssistantWatchItem } from "../api/assistant-watch"
import { pendingSendIdentity } from "../lib/pending-send"
import { taskReceipt } from "../lib/task-receipt"
import { TASK_STATUS_DOT, TASK_STATUS_TONE, plainSummary, sinceLabel, taskStatus, type TaskStatus } from "../lib/task-status"

interface ReceiptsProps { parts: MessagePart[] }
/** Cards for the tasks this turn started or changed, one per task. */
export function AssistantTaskReceipts({ parts }: ReceiptsProps) {
  const seen = new Set<string>()
  const tasks = parts.filter((part): part is ToolPart => part.type === "tool").flatMap((part) => {
    const receipt = taskReceipt(part)
    if (!receipt || seen.has(receipt.taskId)) return []
    seen.add(receipt.taskId)
    return [receipt.taskId]
  })
  return <>{tasks.map((taskId) => <AssistantTaskCard key={taskId} taskId={taskId} />)}</>
}

/** The task conversation's own final reply, read on demand. */
function FullResult({ resultId }: { resultId: string }) {
  const { t } = useTranslation("chat")
  const query = useAssistantResult(resultId, true)
  const errorMessage = useApiErrorMessage()
  if (query.error) return <p role="alert" className="text-dangerink mt-3 text-sm">{errorMessage(query.error)}</p>
  if (query.isPending) return <p role="status" className="text-n600 mt-3 text-sm">{t("assistant.card.loadingResult")}</p>
  const sources = query.data?.pages.flatMap((page) => page.sources) ?? []
  return <div className="bg-hairsoft mt-3 space-y-3 rounded-xl p-3 text-sm">
    {sources.length === 0 && <p className="text-n600">{t("assistant.card.noResultText")}</p>}
    {sources.map((source, index) => <p key={`${source.part_id}:${index}`} className="whitespace-pre-wrap break-words leading-relaxed">{source.text}</p>)}
    {query.hasNextPage && <button type="button" className="text-n700 underline underline-offset-2" disabled={query.isFetchingNextPage}
      onClick={() => void query.fetchNextPage()}>{t("assistant.card.moreResult")}</button>}
  </div>
}

/** Pause / resume / cancel through the existing control command, one at a time. */
function useTaskControl(value: AssistantTaskView) {
  const { t } = useTranslation("chat")
  const control = useAssistantControl()
  const errorMessage = useApiErrorMessage()
  const sending = useRef(false)
  const [busy, setBusy] = useState(false)
  const { task, run_binding: binding } = value
  const submit = async (action: AssistantControlAction) => {
    if (sending.current) return
    sending.current = true
    setBusy(true)
    const user = useAuthStore.getState().user?.id
    const workspace = useWorkspaceStore.getState().currentId
    const run = binding?.run_id && binding.phase !== "idle" ? { run_id: binding.run_id, generation: binding.generation } : null
    try {
      const identity = await pendingSendIdentity(JSON.stringify([user, workspace, "task-control"]), {
        text: JSON.stringify([task.id, action, task.control_revision, run]),
      })
      if (user !== useAuthStore.getState().user?.id || workspace !== useWorkspaceStore.getState().currentId) return
      try {
        await control.mutateAsync({ taskId: task.id, action, revision: task.control_revision, run, key: identity.id })
        identity.confirmed()
        toast("info", t(`assistant.card.controlDone.${action}`))
      } catch (error) {
        if (error instanceof ApiError && error.status < 500) identity.confirmed()
        if (error instanceof ApiError && error.code === "ASSISTANT_EFFECT_UNRESOLVED") toast("info", t("assistant.card.checkEffects"))
        else if (error instanceof ApiError && error.status === 409) toast("info", t("assistant.card.changed"))
        else toast("error", errorMessage(error))
      }
    } catch (error) { toast("error", errorMessage(error)) }
    finally { sending.current = false; setBusy(false) }
  }
  return { submit, busy: busy || control.isPending }
}

function TaskMenu({ value, result, showResult, onToggleResult }: {
  value: AssistantTaskView; result: AssistantResult | null; showResult: boolean; onToggleResult: () => void
}) {
  const { t } = useTranslation("chat")
  const [open, setOpen] = useState(false)
  const { submit, busy } = useTaskControl(value)
  const archive = useAssistantArchive()
  const retry = useRetryAssistantReport()
  const errorMessage = useApiErrorMessage()
  const { task } = value
  const run = (action: () => void) => () => { setOpen(false); action() }
  const retryReport = async () => {
    if (!result || retry.isPending) return
    const identity = await pendingSendIdentity(JSON.stringify([useAuthStore.getState().user?.id,
      useWorkspaceStore.getState().currentId, "report-retry"]), { text: `${result.result_id}:${result.report_attempt}` })
    try {
      await retry.mutateAsync({ resultId: result.result_id, attempt: result.report_attempt, key: identity.id })
      identity.confirmed()
    } catch (error) { toast("error", errorMessage(error)) }
  }
  const canPause = task.desired_state === "running" && !task.archived_at
  const canResume = task.desired_state === "paused" && ["paused", "resume_blocked"].includes(task.observed_state)
  const canCancel = task.desired_state !== "canceled" && !task.archived_at
  const canRetry = !!result && ["blocked", "retry_wait"].includes(result.delivery_state)
  return (
    <div className="relative">
      <button type="button" aria-label={t("assistant.card.more")} title={t("assistant.card.more")} aria-expanded={open}
        disabled={busy || archive.isPending} onClick={() => setOpen(!open)}
        className="text-n700 hover:bg-hairsoft flex size-8 items-center justify-center rounded-full disabled:opacity-50">
        <MoreHorizontal size={17} strokeWidth={2.2} />
      </button>
      <Menu open={open} onClose={() => setOpen(false)} className="end-0 top-9 min-w-40">
        {result && <MenuItem onClick={run(onToggleResult)}>{t(showResult ? "assistant.card.hideResult" : "assistant.card.showResult")}</MenuItem>}
        {canRetry && <MenuItem onClick={run(() => void retryReport())}>{t("assistant.card.retryReport")}</MenuItem>}
        {canPause && <MenuItem onClick={run(() => void submit("pause"))}>{t("assistant.card.pause")}</MenuItem>}
        {canResume && <MenuItem onClick={run(() => void submit("resume"))}>{t("assistant.card.resume")}</MenuItem>}
        {canCancel && <MenuItem danger onClick={run(() => void submit("cancel"))}>{t("assistant.card.cancel")}</MenuItem>}
        {!task.archived_at && <MenuItem onClick={run(() => archive.mutate({ taskId: task.id, revision: task.control_revision }, {
          onSuccess: (receipt) => { if (receipt) toast("info", t("assistant.card.unfollowed")) },
          onError: (error) => toast("error", errorMessage(error)),
        }))}>{t("assistant.card.unfollow")}</MenuItem>}
      </Menu>
    </div>
  )
}

/** A note under the summary only when something needs explaining. */
function taskNote(value: AssistantTaskView, result: AssistantResult | null): string | null {
  const { task, latest_submission: submission } = value
  if (task.observed_state === "effect_unknown") return "assistant.card.note.effectUnknown"
  if (task.observed_state === "resume_blocked") return "assistant.card.note.resumeBlocked"
  if (result?.delivery_state === "blocked") return result.last_error_code === "user_stopped"
    ? "assistant.card.note.reportStopped" : "assistant.card.note.reportFailed"
  if (submission?.disposition === "not_applied") return "assistant.card.note.steerNotApplied"
  if (submission?.state === "canceled" && !submission.applied_at) {
    return submission.error?.code === "ASSISTANT_ASSET_UNAVAILABLE" ? "assistant.card.note.assetUnavailable"
      : "assistant.card.note.inputCanceled"
  }
  return null
}

/** The lines under a task's title: the latest word, and only the notes that need saying. */
function CardDetails({ status, summary, value, note }: {
  status: TaskStatus; summary: string | null; value: AssistantTaskView; note: string | null
}) {
  const { t } = useTranslation("chat")
  const { continuation, desired_state: desired } = value.task
  const following = continuation?.state === "active" && desired !== "paused"
  const decision = continuation && ["needs_decision", "exhausted"].includes(continuation.state)
  return <>
    {summary && <p className="text-n800 mt-2 line-clamp-3 text-sm leading-relaxed whitespace-pre-line">{summary}</p>}
    {status === "waiting" && <p className="text-n800 mt-2 text-sm">{t("assistant.card.waitingHint")}</p>}
    {following && <p className="text-n600 mt-2 text-xs">{t("assistant.card.followingUp", {
      used: continuation.followups_used, limit: continuation.max_followups })}</p>}
    {decision && <p className="text-n800 mt-2 text-sm">{t(`assistant.card.continuation.${continuation.state}`)}</p>}
    {note && <p className="text-n700 mt-2 text-sm">{t(note)}</p>}
  </>
}

function cardStatus(value: AssistantTaskView, watch: AssistantWatchItem | undefined, result: AssistantResult | null): TaskStatus {
  return taskStatus({ sessionStatus: value.execution_session.status, observedState: value.task.observed_state,
    desiredState: value.task.desired_state, pendingQuestions: watch?.pending_questions ?? 0, outcome: result?.outcome })
}

/** The task, its conversation or its project was deleted: nothing left to show or control. */
const GONE = new Set(["ASSISTANT_EXECUTION_UNAVAILABLE", "ASSISTANT_TASK_UNAVAILABLE", "ASSISTANT_PROJECT_UNAVAILABLE"])

interface CardProps { taskId: string; initial?: AssistantTaskView; selectedResult?: AssistantResult; className?: string }
export function AssistantTaskCard({ taskId, initial, selectedResult, className }: CardProps) {
  const { t, i18n } = useTranslation("chat")
  const query = useAssistantTask(taskId, !initial)
  // The watch list already carries the project name, the newest summary and
  // pending questions; a task no longer followed simply goes without them.
  const watch = useAssistantWatch().data?.items.find((item) => item.task_id === taskId)
  const [showResult, setShowResult] = useState(false)
  const errorMessage = useApiErrorMessage()
  const value = initial ?? query.data
  if (!initial && query.error) {
    if (query.error instanceof ApiError && GONE.has(query.error.code)) {
      return <p className="border-hair text-n600 my-2 rounded-2xl border border-dashed px-4 py-3 text-sm">{t("assistant.card.gone")}</p>
    }
    return <div role="alert" className="border-hair text-n700 my-2 rounded-2xl border p-4 text-sm">{errorMessage(query.error)}</div>
  }
  if (!value) return <div role="status" className="text-n600 my-2 text-sm">{t("assistant.card.loading")}</div>
  const { task } = value
  const result = selectedResult ?? value.latest_result
  const status = cardStatus(value, watch, result)
  // A notification can point at an older run; say so rather than pass it off as the latest.
  const earlier = !!selectedResult && selectedResult.result_id !== value.latest_result?.result_id
  const summary = selectedResult ? null : plainSummary(watch?.latest_result?.summary ?? "") || null
  return (
    <section aria-label={t("assistant.card.label", { title: task.title })}
      className={cn("border-hair bg-card my-2 rounded-2xl border p-4", className)}>
      <div className="flex items-start gap-3">
        <span className={cn("mt-2 size-2 flex-none rounded-full", TASK_STATUS_DOT[status])} aria-hidden />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <h3 className="text-ink min-w-0 text-base font-medium break-words">{task.title}</h3>
            <StatusPill tone={TASK_STATUS_TONE[status]}>{t(`assistant.status.${status}`)}</StatusPill>
          </div>
          <p className="text-n600 mt-0.5 text-sm">
            {[watch?.project.name, sinceLabel(task.updated_at, i18n?.language)].filter(Boolean).join(" · ")}
          </p>
          <CardDetails status={status} summary={summary} value={value}
            note={earlier ? "assistant.card.note.earlier" : taskNote(value, result)} />
        </div>
        <TaskMenu value={value} result={result} showResult={showResult} onToggleResult={() => setShowResult(!showResult)} />
      </div>
      <div className="mt-3 flex items-center gap-2 ps-5">
        <Link to={paths.chat(task.execution_session_id)}
          className="border-hair text-n800 hover:bg-hairsoft inline-flex items-center gap-1 rounded-full border px-3 py-1 text-sm">
          {t(status === "waiting" ? "assistant.card.answer" : "assistant.card.open")}
          <ArrowUpRight size={14} strokeWidth={2.2} aria-hidden />
        </Link>
      </div>
      {showResult && result && <div className="ps-5"><FullResult key={result.result_id} resultId={result.result_id} /></div>}
    </section>
  )
}
