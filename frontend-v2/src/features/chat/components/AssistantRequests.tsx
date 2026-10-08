import { useEffect, type ReactNode } from "react"
import { useInfiniteQuery, useQuery, useQueryClient } from "@tanstack/react-query"
import { Link } from "react-router"
import { BellRing } from "lucide-react"
import { useTranslation } from "react-i18next"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import { http } from "@/shared/api/http"
import { paths } from "@/shared/router/paths"
import { wsClient } from "@/shared/ws/client"
import type { WsEventName } from "@/shared/ws/events"
import type { PermissionRequest, QuestionRequest } from "@/shared/types/api"
import { useApiErrorMessage } from "@/shared/hooks/useApiErrorMessage"
import { assistantKeys, scopedOptions } from "../api/assistant"
import type { QuestionReceipt } from "../api/question"
import { QuestionDock } from "./QuestionDock"
import { PermissionCard } from "./PermissionCard"
import { AssistantRequestReview } from "./AssistantRequestReview"

interface RequestPage<T> {
  items: Array<T & { task_title: string; project_name?: string }>
  next_cursor: string | null
  receipts: QuestionReceipt[]
}

/** A question waiting in one of the user's other conversations (not watched). */
interface WaitingQuestion {
  id: string
  session_id: string
  session_title: string
  project_name?: string | null
  questions: Array<{ header: string; question: string }>
}

/** Socket events that add, change or settle a pending request of each kind. */
const REQUEST_EVENTS: Record<"question" | "permission", readonly WsEventName[]> = {
  question: ["question.asked", "question.updated", "question.replied", "question.rejected", "question.cancelled"],
  permission: ["permission.asked", "permission.replied"],
}

function useRequests<T>(kind: "question" | "permission") {
  const userId = useAuthStore((state) => state.user?.id ?? "anonymous")
  const workspaceId = useWorkspaceStore((state) => state.currentId)
  const qc = useQueryClient()
  const enabled = userId !== "anonymous" && !!workspaceId
  useEffect(() => {
    if (!enabled) return
    const refresh = () => void qc.invalidateQueries({ queryKey: [...assistantKeys.requests(userId, workspaceId), kind] })
    const off = REQUEST_EVENTS[kind].map((event) => wsClient.on(event, refresh))
    return () => off.forEach((stop) => stop())
  }, [qc, userId, workspaceId, kind, enabled])
  return useInfiniteQuery({
    queryKey: [...assistantKeys.requests(userId, workspaceId), kind],
    initialPageParam: null as string | null,
    queryFn: ({ pageParam, signal }) => http.get<RequestPage<T>>(`/api/assistant/requests?kind=${kind}${pageParam
      ? `&cursor=${encodeURIComponent(pageParam)}` : ""}`, scopedOptions(workspaceId, signal)),
    getNextPageParam: (page) => page.next_cursor ?? undefined,
    // Socket events and assistant events invalidate these; the interval is a fallback.
    refetchInterval: 15_000,
    enabled,
  })
}

/** Questions in the user's other conversations: answered there, or by the assistant on request. */
function useWaiting() {
  const userId = useAuthStore((state) => state.user?.id ?? "anonymous")
  const workspaceId = useWorkspaceStore((state) => state.currentId)
  const qc = useQueryClient()
  const enabled = userId !== "anonymous" && !!workspaceId
  const key = [...assistantKeys.requests(userId, workspaceId), "waiting"]
  useEffect(() => {
    if (!enabled) return
    const refresh = () => void qc.invalidateQueries({ queryKey: [...assistantKeys.requests(userId, workspaceId), "waiting"] })
    const off = REQUEST_EVENTS.question.map((event) => wsClient.on(event, refresh))
    return () => off.forEach((stop) => stop())
  }, [qc, userId, workspaceId, enabled])
  return useQuery({
    queryKey: key,
    queryFn: ({ signal }) => http.get<{ items: WaitingQuestion[] }>("/api/assistant/requests/waiting",
      scopedOptions(workspaceId, signal)),
    refetchInterval: 30_000,
    enabled,
  })
}

/** What waits on the user across the conversations the assistant follows,
 *  shown at the end of the assistant conversation like a secretary's "these
 *  need you": the actual question or approval cards, plus questions waiting
 *  in other conversations. Nothing renders when nothing waits. */
export function AssistantRequests({ renderQuestion }: { renderQuestion?: (request: QuestionRequest) => ReactNode }) {
  const { t } = useTranslation("chat")
  const errorMessage = useApiErrorMessage()
  const requests = useRequests<QuestionRequest>("question")
  const approvals = useRequests<PermissionRequest>("permission")
  const items = [...new Map(requests.data?.pages.flatMap((page) => page.items.map((item) => [item.id, item] as const))).values()]
  const permissions = [...new Map(approvals.data?.pages.flatMap((page) => page.items.map((item) => [item.id, item] as const))).values()]
  // A reply that could not be applied is the only receipt worth a line.
  const failed = [...requests.data?.pages[0]?.receipts ?? [], ...approvals.data?.pages[0]?.receipts ?? []]
    .filter((receipt) => receipt.state === "failed")
  const waiting = useWaiting().data?.items ?? []
  const error = requests.error ?? approvals.error
  const count = items.length + permissions.length + waiting.length
  if (!error && !count && !failed.length && !requests.hasNextPage && !approvals.hasNextPage) return null
  return <section aria-label={t("assistant.requests.label")} className="border-hair bg-a100 space-y-3 rounded-2xl border p-4">
    <div className="flex items-center gap-2">
      <BellRing size={16} strokeWidth={2.2} className="text-accent flex-none" aria-hidden />
      <h2 className="text-ink text-base font-medium">{t("assistant.requests.title", { count })}</h2>
    </div>
    {error ? <p role="alert" className="text-dangerink text-sm">{errorMessage(error)}</p> : <>
      {items.map((request) => <div key={request.id} className="border-hair bg-card rounded-xl border p-3">
        <RequestSource title={request.task_title} project={request.project_name} sessionId={request.session_id} kind="question" />
        {renderQuestion ? renderQuestion(request) : <QuestionDock request={request} />}
        {request.assistant && <AssistantRequestReview requestId={request.id} binding={request.assistant} />}
      </div>)}
      {requests.hasNextPage && <button type="button" className="text-n700 text-sm underline underline-offset-2" disabled={requests.isFetchingNextPage}
        onClick={() => void requests.fetchNextPage()}>{t("assistant.requests.more")}</button>}
      {permissions.map((request) => <div key={request.id} className="border-hair bg-card rounded-xl border p-3">
        <RequestSource title={request.task_title} project={request.project_name} sessionId={request.session_id} kind="permission" />
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
            to={paths.chat(item.session_id)}>{t("assistant.requests.answerThere")}</Link>
        </div>)}
        <p className="text-n600 text-xs">{t("assistant.requests.askMe")}</p>
      </div>}
      {failed.map((receipt) => <p key={receipt.command_id} className="text-n700 text-sm">
        {t("assistant.requests.failed")}
        {receipt.session_id && <Link className="ms-2 underline underline-offset-2" to={paths.chat(receipt.session_id)}>{t("assistant.requests.openTask")}</Link>}
      </p>)}
    </>}
  </section>
}

function RequestSource({ title, project, sessionId, kind }: { title: string; project?: string; sessionId: string; kind: "question" | "permission" }) {
  const { t } = useTranslation("chat")
  return <div className="mb-2 flex items-center justify-between gap-3 text-sm">
    <span className="text-n700 min-w-0 truncate">
      {t(kind === "question" ? "assistant.requests.asks" : "assistant.requests.needsApproval", { title })}
      {project ? <span className="text-n600"> · {project}</span> : null}
    </span>
    <Link className="text-n700 flex-none text-xs underline underline-offset-2" to={paths.chat(sessionId)}>{t("assistant.requests.openTask")}</Link>
  </div>
}
