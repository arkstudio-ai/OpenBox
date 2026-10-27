import { useId, useState, type ReactNode } from "react"
import { Link, useMatch, useNavigate } from "react-router"
import { BellRing, ChevronDown, X } from "lucide-react"
import { useTranslation } from "react-i18next"
import { paths } from "@/shared/router/paths"
import type { QuestionRequest } from "@/shared/types/api"
import { useApiErrorMessage } from "@/shared/hooks/useApiErrorMessage"
import { useAssistantRequests } from "../api/assistant-requests"
import { useAssistantTasksPanel } from "../stores/assistant-tasks-panel"
import { plainSummary } from "../lib/task-status"
import { QuestionDock } from "./QuestionDock"
import { PermissionCard } from "./PermissionCard"
import { AssistantRequestReview } from "./AssistantRequestReview"

/** Pending questions and approvals in My tasks, or a dismissible chat hint.
 *  Includes unanswered questions in the user's other conversations.
 *  ``conversation``: the questions and approvals of tasks the assistant
 *  delegated stay cards at the end of its conversation, answerable there;
 *  only the rest (other conversations' questions) is a hint to My tasks. */
export function AssistantRequests({ renderQuestion, compact = false, conversation = false, onNavigate }: {
  renderQuestion?: (request: QuestionRequest) => ReactNode; compact?: boolean; conversation?: boolean; onNavigate?: () => void
}) {
  const { t } = useTranslation("chat")
  const errorMessage = useApiErrorMessage()
  const data = useAssistantRequests()
  const drawerOpen = useAssistantTasksPanel((state) => state.open)
  const { requests, approvals, items, permissions, waiting, failed, error, count } = data
  // My tasks shows the same cards while it is open: two editors for one card would race its draft.
  if (compact || (conversation && drawerOpen)) return <RequestReminder key={data.scopeKey} data={data} />
  if (conversation) {
    const shown = new Set([...items.map((item) => `q:${item.id}`), ...permissions.map((item) => `p:${item.id}`)])
    const rest = data.ids.filter((id) => !shown.has(id))
    const restCount = rest.filter((id) => id.startsWith("q:") || id.startsWith("p:")).length
    return <>
      {shown.size > 0 && !error && <section aria-label={t("assistant.requests.label")} className="border-hair bg-a100 space-y-3 rounded-2xl border p-4">
        <RequestsHeading count={shown.size} />
        <RequestCards data={data} renderQuestion={renderQuestion} onNavigate={onNavigate} />
      </section>}
      <RequestReminder key={data.scopeKey} data={{ ...data, ids: rest, count: restCount }} />
    </>
  }
  if (!error && !count && !failed.length && !requests.hasNextPage && !approvals.hasNextPage) return null
  return <section aria-label={t("assistant.requests.label")} className="border-hair bg-a100 space-y-3 rounded-2xl border p-4">
    <RequestsHeading count={count} />
    {error ? <p role="alert" className="text-dangerink text-sm">{errorMessage(error)}</p> : <>
      <RequestCards data={data} renderQuestion={renderQuestion} onNavigate={onNavigate} />
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

function RequestsHeading({ count }: { count: number }) {
  const { t } = useTranslation("chat")
  return <div className="flex items-center gap-2">
    <BellRing size={16} strokeWidth={2.2} className="text-accent flex-none" aria-hidden />
    <h2 className="text-dangerink text-base font-medium">{t("assistant.requests.title", { count })}</h2>
  </div>
}

type Requests = ReturnType<typeof useAssistantRequests>
type RequestCard = { key: string; kind: "question"; request: Requests["items"][number] }
  | { key: string; kind: "permission"; request: Requests["permissions"][number] }

/** The bound questions and approvals, each answerable as in its own task.
 *  Like a mail thread, in the order they were asked, only the newest is open
 *  and the rest fold to a line each; once it is answered, the next one opens. */
function RequestCards({ data, renderQuestion, onNavigate }: {
  data: Requests; renderQuestion?: (request: QuestionRequest) => ReactNode; onNavigate?: () => void
}) {
  const { t } = useTranslation("chat")
  const prefix = useId()
  const { requests, approvals, items, permissions } = data
  // A card the user opened or began answering stays open until it is answered:
  // one arriving meanwhile folds instead of taking the card being answered away.
  const [chosen, setChosen] = useState<string | null>(null)
  const cards: RequestCard[] = [
    ...items.map((request) => ({ key: `q:${request.id}`, kind: "question" as const, request })),
    ...permissions.map((request) => ({ key: `p:${request.id}`, kind: "permission" as const, request })),
  ].sort((a, b) => askedAt(a.request.created_at) - askedAt(b.request.created_at))
  const open = cards.find((card) => card.key === chosen) ?? cards[cards.length - 1]
  return <>
    {cards.map((card) => {
      const expanded = card === open
      const keep = expanded ? () => setChosen(card.key) : undefined
      return <div key={card.key} onFocusCapture={keep} onPointerDownCapture={keep}
        className={`border-hair bg-card rounded-xl border ${expanded ? "p-3" : "px-3 py-2"}`}>
        <RequestSource title={card.request.task_title} project={card.request.project_name} sessionId={card.request.session_id}
          kind={card.kind} onNavigate={onNavigate} panel={prefix + card.key} expanded={expanded} onExpand={() => setChosen(card.key)}
          preview={card.kind === "question" ? plainSummary(card.request.questions[0]?.question ?? "") : card.request.title ?? card.request.tool} />
        {expanded && <div id={prefix + card.key}>
          {card.kind === "permission" ? <PermissionCard request={card.request} />
            : renderQuestion ? renderQuestion(card.request) : <QuestionDock request={card.request} />}
          {card.request.assistant && <AssistantRequestReview requestId={card.request.id} binding={card.request.assistant} />}
        </div>}
      </div>
    })}
    {requests.hasNextPage && <button type="button" className="text-n700 text-sm underline underline-offset-2" disabled={requests.isFetchingNextPage}
      onClick={() => void requests.fetchNextPage()}>{t("assistant.requests.more")}</button>}
    {approvals.hasNextPage && <button type="button" className="text-n700 text-sm underline underline-offset-2" disabled={approvals.isFetchingNextPage}
      onClick={() => void approvals.fetchNextPage()}>{t("assistant.requests.morePermissions")}</button>}
  </>
}

function askedAt(createdAt?: string) {
  return Date.parse(createdAt ?? "") || 0
}

/** Who asks, as an accordion header: a folded card adds its question and opens on click. */
function RequestSource({ title, project, sessionId, kind, preview, expanded, panel, onExpand, onNavigate }: {
  title: string; project?: string; sessionId: string; kind: "question" | "permission"; preview?: string
  expanded: boolean; panel: string; onExpand: () => void; onNavigate?: () => void
}) {
  const { t } = useTranslation("chat")
  return <div className={`flex items-start justify-between gap-3 text-sm ${expanded ? "mb-2" : ""}`}>
    {/* One card is always open, so its header cannot fold it (WAI-ARIA accordion). */}
    <button type="button" aria-expanded={expanded} aria-controls={expanded ? panel : undefined} aria-disabled={expanded || undefined}
      onClick={expanded ? undefined : onExpand} className={`flex min-w-0 flex-1 items-start gap-2 text-start ${expanded ? "cursor-default" : ""}`}>
      <span className="min-w-0 flex-1">
        <span className="text-n700 block truncate">
          {t(kind === "question" ? "assistant.requests.asks" : "assistant.requests.needsApproval", { title })}
          {project ? <span className="text-n600"> · {project}</span> : null}
        </span>
        {!expanded && preview && <span className="text-ink mt-0.5 block truncate">{preview}</span>}
      </span>
      {!expanded && <ChevronDown size={16} className="text-n600 mt-0.5 flex-none" aria-hidden />}
    </button>
    <Link onClick={onNavigate} className="text-n700 flex-none text-xs underline underline-offset-2" to={paths.chat(sessionId)}>{t("assistant.requests.openTask")}</Link>
  </div>
}

/** Closing this hint only changes local presentation; the requests remain in My tasks. */
function RequestReminder({ data }: { data: ReturnType<typeof useAssistantRequests> }) {
  const { t } = useTranslation("chat")
  const setOpen = useAssistantTasksPanel((state) => state.setOpen)
  // My tasks is mounted on the assistant page only; from the floating companion, go there.
  const onAssistantPage = useMatch(paths.assistant) !== null
  const navigate = useNavigate()
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
    <button type="button" onClick={() => { setOpen(true); if (!onAssistantPage) void navigate(paths.assistant) }}
      className="text-dangerink min-w-0 flex-1 text-start text-sm">
      {data.count ? t("assistant.requests.reminder", { count: data.count }) : t("assistant.requests.reviewTasks")}
    </button>
    <button type="button" onClick={dismiss} aria-label={t("assistant.requests.dismissReminder")}
      title={t("assistant.requests.dismissReminder")} className="text-n600 hover:bg-hairsoft flex size-8 flex-none items-center justify-center rounded-full">
      <X size={16} aria-hidden />
    </button>
  </section>
}
