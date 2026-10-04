import { useState } from "react"
import { Link } from "react-router"
import { useTranslation } from "react-i18next"
import type { MessagePart, ToolPart } from "@/shared/types/api"
import { paths } from "@/shared/router/paths"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import { useApiErrorMessage } from "@/shared/hooks/useApiErrorMessage"
import { toast } from "@/shared/ui/Toast"
import { useAssistantResult, useAssistantTask, useRetryAssistantReport, type AssistantTaskView, type AssistantResult } from "../api/assistant"
import { pendingSendIdentity } from "../lib/pending-send"
import { AssistantResultFacts } from "./AssistantResultFacts"
import { taskReceipt } from "../lib/task-receipt"
import { AssistantTaskControls } from "./AssistantTaskControls"

interface ReceiptsProps { parts: MessagePart[] }
export function AssistantTaskReceipts({ parts }: ReceiptsProps) {
  const receipts = parts.filter((part): part is ToolPart => part.type === "tool").flatMap((part) => {
    const receipt = taskReceipt(part)
    return receipt ? [receipt] : []
  })
  return <>{receipts.map((receipt) => <AssistantTaskCard key={receipt.commandId} taskId={receipt.taskId} commandId={receipt.commandId} />)}</>
}

interface ReportProps { resultId: string }
function SourceReport({ resultId }: ReportProps) {
  const { t } = useTranslation("chat")
  const query = useAssistantResult(resultId, true)
  const errorMessage = useApiErrorMessage()
  if (query.error) return <p role="alert" className="text-dangerink text-sm">{errorMessage(query.error)}</p>
  // Cached text waits for current source validation whenever this panel is reopened/refetched.
  if (query.isPending || query.isFetching && !query.isFetchingNextPage) return <p role="status">{t("assistant.loadingReport")}</p>
  return <div className="mt-2 space-y-3 text-sm">
    {query.data?.pages.flatMap((page) => page.sources.map((source, index) => <div key={`${source.part_id}:${page.offset}:${index}`}>
      <Link className="text-n600 underline" to={paths.chat(source.session_id)}>{t("assistant.openSource")}</Link>
      <p className="mt-1 whitespace-pre-wrap break-words">{source.text}</p>
    </div>))}
    {query.hasNextPage && <button type="button" className="underline" disabled={query.isFetchingNextPage}
      onClick={() => void query.fetchNextPage()}>{t("assistant.moreReport")}</button>}
  </div>
}

interface CardProps { taskId: string; commandId?: string; initial?: AssistantTaskView; selectedResult?: AssistantResult }
function submissionLabel(submission: NonNullable<AssistantTaskView["latest_submission"]>) {
  if (submission.disposition === "not_applied") return "assistant.steerNotApplied"
  if (submission.state === "canceled" && !submission.applied_at) {
    return submission.error?.code === "ASSISTANT_ASSET_UNAVAILABLE" ? "assistant.inputAssetUnavailable" : "assistant.inputCanceled"
  }
  if (submission.delivery === "steer") return submission.applied_at ? "assistant.steerApplied" : "assistant.steerAccepted"
  return submission.applied_at ? "assistant.inputApplied" : "assistant.inputAccepted"
}

export function AssistantTaskCard({ taskId, commandId, initial, selectedResult }: CardProps) {
  const { t } = useTranslation("chat")
  const query = useAssistantTask(taskId, !initial)
  const retry = useRetryAssistantReport()
  const [reportOpen, setReportOpen] = useState(false)
  const errorMessage = useApiErrorMessage()
  const value = initial ?? query.data
  if (!initial && query.error) return <div role="alert" className="border-hair my-2 rounded-xl border p-3 text-sm">{errorMessage(query.error)}</div>
  if (!value) return <div role="status" className="text-n600 my-2 text-sm">{t("assistant.loadingTask")}</div>
  const { task, latest_submission: submission, execution_session: execution } = value
  const result = selectedResult ?? value.latest_result
  const retryReport = async () => {
    if (!result || retry.isPending) return
    const identity = await pendingSendIdentity(JSON.stringify([useAuthStore.getState().user?.id,
      useWorkspaceStore.getState().currentId, "report-retry"]), { text: `${result.result_id}:${result.report_attempt}` })
    try {
      await retry.mutateAsync({ resultId: result.result_id, attempt: result.report_attempt, key: identity.id })
      identity.confirmed()
    } catch (error) { toast("error", errorMessage(error)) }
  }
  return <section className="border-hair bg-n100/50 my-2 rounded-xl border p-4" aria-label={t("assistant.taskCard")}>
    <div className="flex items-start justify-between gap-3">
      <h3 className="min-w-0 break-words text-sm font-semibold">{task.title}</h3>
      <Link className="flex-none text-xs underline" to={paths.chat(task.execution_session_id)}>{t("assistant.openTask")}</Link>
    </div>
    <AssistantTaskControls key={task.id} value={value} />
    {submission && <p className="text-n600 mt-2 text-xs">{t(submissionLabel(submission))}</p>}
    {execution.status === "waiting_input" && task.desired_state !== "canceled" && <p className="mt-2 text-sm">{t("assistant.taskWaiting")}</p>}
    {result && (result.observed_intent_revision < task.intent_revision || result.result_id !== value.latest_result?.result_id) && <p className="text-n600 mt-2 text-xs">{t("assistant.earlierResult")}</p>}
    <AssistantResultFacts result={result} />
    {commandId && <details className="text-n600 mt-3 text-xs"><summary className="cursor-pointer">{t("assistant.receipt")}</summary>
      <p className="mt-1 break-all font-mono">{commandId}</p></details>}
    {result && <div className="mt-3 flex flex-wrap gap-4 text-xs">
      <button type="button" className="underline" aria-expanded={reportOpen} onClick={() => setReportOpen(!reportOpen)}>{t("assistant.originalReport")}</button>
      {["blocked", "retry_wait"].includes(result.delivery_state) && <button type="button" className="underline" disabled={retry.isPending}
        onClick={() => void retryReport()}>{retry.isPending ? t("assistant.retryPending") : t("assistant.retryReport")}</button>}
    </div>}
    {result?.delivery_state === "blocked" && <p className="text-n600 mt-2 text-xs">{result.last_error_code === "user_stopped"
      ? t("assistant.reportStopped") : t("assistant.reportFailure")}</p>}
    {reportOpen && result && <SourceReport key={result.result_id} resultId={result.result_id} />}
  </section>
}
