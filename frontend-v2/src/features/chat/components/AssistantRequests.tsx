import { useState, type ReactNode } from "react"
import { Link } from "react-router"
import { BellRing, X } from "lucide-react"
import { useTranslation } from "react-i18next"
import { paths } from "@/shared/router/paths"
import type { QuestionRequest } from "@/shared/types/api"
import { useApiErrorMessage } from "@/shared/hooks/useApiErrorMessage"
import { useAssistantRequests } from "../api/assistant-requests"
import { useAssistantTasksPanel } from "../stores/assistant-tasks-panel"
import { QuestionDock } from "./QuestionDock"
import { PermissionCard } from "./PermissionCard"
import { AssistantRequestReview } from "./AssistantRequestReview"

/** Pending questions and approvals in My tasks, or a dismissible chat hint.
 *  Includes unanswered questions in the user's other conversations. */
export function AssistantRequests({ renderQuestion, compact = false, onNavigate }: {
  renderQuestion?: (request: QuestionRequest) => ReactNode; compact?: boolean; onNavigate?: () => void
}) {
  const { t } = useTranslation("chat")
  const errorMessage = useApiErrorMessage()
  const data = useAssistantRequests()
  const { requests, approvals, items, permissions, waiting, failed, error, count } = data
  if (compact) return <RequestReminder key={data.scopeKey} data={data} />
  if (!error && !count && !failed.length && !requests.hasNextPage && !approvals.hasNextPage) return null
  return <section aria-label={t("assistant.requests.label")} className="border-hair bg-a100 space-y-3 rounded-2xl border p-4">
    <div className="flex items-center gap-2">
      <BellRing size={16} strokeWidth={2.2} className="text-accent flex-none" aria-hidden />
      <h2 className="text-dangerink text-base font-medium">{t("assistant.requests.title", { count })}</h2>
    </div>
    {error ? <p role="alert" className="text-dangerink text-sm">{errorMessage(error)}</p> : <>
      {items.map((request) => <div key={request.id} className="border-hair bg-card rounded-xl border p-3">
        <RequestSource title={request.task_title} project={request.project_name} sessionId={request.session_id} kind="question" onNavigate={onNavigate} />
        {renderQuestion ? renderQuestion(request) : <QuestionDock request={request} />}
        {request.assistant && <AssistantRequestReview requestId={request.id} binding={request.assistant} />}
      </div>)}
      {requests.hasNextPage && <button type="button" className="text-n700 text-sm underline underline-offset-2" disabled={requests.isFetchingNextPage}
        onClick={() => void requests.fetchNextPage()}>{t("assistant.requests.more")}</button>}
      {permissions.map((request) => <div key={request.id} className="border-hair bg-card rounded-xl border p-3">
        <RequestSource title={request.task_title} project={request.project_name} sessionId={request.session_id} kind="permission" onNavigate={onNavigate} />
        <PermissionCard request={request} />
        {request.assistant && <AssistantRequestReview requestId={request.id} binding={request.assistant} />}
      </div>)}
      {approvals.hasNextPage && <button type="button" className="text-n700 text-sm underline underline-offset-2" disabled={approvals.isFetchingNextPage}
        onClick={() => void approvals.fetchNextPage()}>{t("assistant.requests.morePermissions")}</button>}
      {waiting.length > 0 && <div aria-label={t("assistant.requests.otherConversations")} className="space-y-2">
        <p className="text-n700 text-sm">{t("assistant.requests.otherConversations")}</p>
        {waiting.map((item) => <div key={item.id} className="border-hair bg-card flex items-start justify-between gap-3 rounded-xl border p-3 text-sm">
          <div className="min-w-0">
            <p className="text-n600 truncate text-xs">{item.session_title || t("assistant.requests.untitled")}{item.project_name ? ` · ${item.project_name}` : ""}</p>
            <p className="text-ink mt-0.5 line-clamp-2">{item.questions[0]?.question ?? ""}</p>
          </div>
          <Link className="border-hair text-n800 hover:bg-hairsoft flex-none rounded-full border px-3 py-1 text-xs"
            onClick={onNavigate} to={paths.chat(item.session_id)}>{t("assistant.requests.answerThere")}</Link>
        </div>)}
        <p className="text-n600 text-xs">{t("assistant.requests.askMe")}</p>
      </div>}
      {failed.map((receipt) => <p key={receipt.command_id} className="text-n700 text-sm">
        {t("assistant.requests.failed")}
        {receipt.session_id && <Link onClick={onNavigate} className="ms-2 underline underline-offset-2" to={paths.chat(receipt.session_id)}>{t("assistant.requests.openTask")}</Link>}
      </p>)}
    </>}
  </section>
}

function RequestSource({ title, project, sessionId, kind, onNavigate }: {
  title: string; project?: string; sessionId: string; kind: "question" | "permission"; onNavigate?: () => void
}) {
  const { t } = useTranslation("chat")
  return <div className="mb-2 flex items-center justify-between gap-3 text-sm">
    <span className="text-n700 min-w-0 truncate">
      {t(kind === "question" ? "assistant.requests.asks" : "assistant.requests.needsApproval", { title })}
      {project ? <span className="text-n600"> · {project}</span> : null}
    </span>
    <Link onClick={onNavigate} className="text-n700 flex-none text-xs underline underline-offset-2" to={paths.chat(sessionId)}>{t("assistant.requests.openTask")}</Link>
  </div>
}

/** Closing this hint only changes local presentation; the requests remain in My tasks. */
function RequestReminder({ data }: { data: ReturnType<typeof useAssistantRequests> }) {
  const { t } = useTranslation("chat")
  const setOpen = useAssistantTasksPanel((state) => state.setOpen)
  const storageKey = `openbox:assistant-reminder:${data.scopeKey}`
  const [dismissed, setDismissed] = useState<string[]>(() => {
    try {
      const value: unknown = JSON.parse(localStorage.getItem(storageKey) ?? "[]")
      return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : []
    } catch { return [] }
  })
  if (!data.ids.length || data.ids.every((id) => dismissed.includes(id))) return null
  const dismiss = () => {
    setDismissed(data.ids)
    try { localStorage.setItem(storageKey, JSON.stringify(data.ids)) } catch { /* Still closes in this session. */ }
  }
  return <section aria-label={t("assistant.requests.label")} className="border-hair bg-a100 flex items-center gap-2 rounded-xl border px-3 py-2">
    <BellRing size={15} className="text-dangerink flex-none" aria-hidden />
    <button type="button" onClick={() => setOpen(true)} className="text-dangerink min-w-0 flex-1 text-start text-sm">
      {data.count ? t("assistant.requests.reminder", { count: data.count }) : t("assistant.requests.reviewTasks")}
    </button>
    <button type="button" onClick={dismiss} aria-label={t("assistant.requests.dismissReminder")}
      title={t("assistant.requests.dismissReminder")} className="text-n600 hover:bg-hairsoft flex size-8 flex-none items-center justify-center rounded-full">
      <X size={16} aria-hidden />
    </button>
  </section>
}
