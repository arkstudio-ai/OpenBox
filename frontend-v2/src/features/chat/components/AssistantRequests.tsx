import { useInfiniteQuery } from "@tanstack/react-query"
import type { ReactNode } from "react"
import { Link } from "react-router"
import { useTranslation } from "react-i18next"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import { http } from "@/shared/api/http"
import { paths } from "@/shared/router/paths"
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

function useRequests<T>(kind: "question" | "permission") {
  const userId = useAuthStore((state) => state.user?.id ?? "anonymous")
  const workspaceId = useWorkspaceStore((state) => state.currentId)
  return useInfiniteQuery({
    queryKey: [...assistantKeys.all(userId, workspaceId), "requests", kind],
    initialPageParam: null as string | null,
    queryFn: ({ pageParam, signal }) => http.get<RequestPage<T>>(`/api/assistant/requests?kind=${kind}${pageParam
      ? `&cursor=${encodeURIComponent(pageParam)}` : ""}`, scopedOptions(workspaceId, signal)),
    getNextPageParam: (page) => page.next_cursor ?? undefined,
    refetchInterval: 5_000,
    enabled: userId !== "anonymous" && !!workspaceId,
  })
}

export function AssistantRequests({ renderQuestion }: { renderQuestion?: (request: QuestionRequest) => ReactNode }) {
  const { t } = useTranslation("chat")
  const errorMessage = useApiErrorMessage()
  const requests = useRequests<QuestionRequest>("question")
  const approvals = useRequests<PermissionRequest>("permission")
  const items = [...new Map(requests.data?.pages.flatMap((page) => page.items.map((item) => [item.id, item] as const))).values()]
  const permissions = [...new Map(approvals.data?.pages.flatMap((page) => page.items.map((item) => [item.id, item] as const))).values()]
  const receipts = [...requests.data?.pages[0]?.receipts ?? [], ...approvals.data?.pages[0]?.receipts ?? []]
    .sort((a, b) => (b.accepted_at ?? "").localeCompare(a.accepted_at ?? ""))
  const error = requests.error ?? approvals.error
  const count = items.length + permissions.length
  if (!error && !count && !receipts.length && !requests.hasNextPage && !approvals.hasNextPage) return null
  return <div className="border-hair flex-none border-b px-5 py-2">
    <details open={count > 0}>
      <summary className="text-n600 cursor-pointer text-sm">{t("assistant.requests.title", { count })}</summary>
      <div className="scr mx-auto max-h-[55vh] max-w-190 space-y-3 overflow-y-auto py-3" aria-label={t("assistant.requests.label")}>
        {error ? <p role="alert" className="text-sm">{errorMessage(error)}</p> : <>
          {items.map((request) => <section key={request.id} className="border-hair rounded-xl border p-3">
            <div className="mb-2 flex items-center justify-between gap-3 text-xs">
              <span>{request.task_title}{request.project_name ? ` · ${request.project_name}` : ""}</span>
              <Link className="underline" to={paths.chat(request.session_id)}>{t("assistant.requests.openTask")}</Link>
            </div>
            {renderQuestion ? renderQuestion(request) : <QuestionDock request={request} />}
            {request.assistant && <AssistantRequestReview requestId={request.id} binding={request.assistant} />}
          </section>)}
          {requests.hasNextPage && <button type="button" className="text-sm underline" disabled={requests.isFetchingNextPage}
            onClick={() => void requests.fetchNextPage()}>{t("assistant.requests.more")}</button>}
          {permissions.map((request) => <section key={request.id} className="border-hair rounded-xl border p-3">
            <div className="mb-2 flex items-center justify-between gap-3 text-xs">
              <span>{request.task_title}{request.project_name ? ` · ${request.project_name}` : ""}</span>
              <Link className="underline" to={paths.chat(request.session_id)}>{t("assistant.requests.openTask")}</Link>
            </div>
            <PermissionCard request={request} />
            {request.assistant && <AssistantRequestReview requestId={request.id} binding={request.assistant} />}
          </section>)}
          {approvals.hasNextPage && <button type="button" className="text-sm underline" disabled={approvals.isFetchingNextPage}
            onClick={() => void approvals.fetchNextPage()}>{t("assistant.requests.morePermissions")}</button>}
          {receipts.length > 0 && <details className="text-n600 text-xs">
            <summary className="cursor-pointer">{t("assistant.requests.receipts")}</summary>
            <ul className="mt-2 space-y-2">{receipts.map((receipt) => <li key={receipt.command_id}>
              <span>{t(`assistant.requests.${receipt.state ?? "accepted"}`)}</span>
              <span className="ml-2 break-all font-mono">{receipt.command_id}</span>
              {receipt.session_id && <Link className="ml-2 underline" to={paths.chat(receipt.session_id)}>{t("assistant.requests.openTask")}</Link>}
            </li>)}</ul>
          </details>}
        </>}
      </div>
    </details>
  </div>
}
