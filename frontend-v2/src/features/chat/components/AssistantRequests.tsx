import { useInfiniteQuery } from "@tanstack/react-query"
import type { ReactNode } from "react"
import { Link } from "react-router"
import { useTranslation } from "react-i18next"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import { http } from "@/shared/api/http"
import { paths } from "@/shared/router/paths"
import type { QuestionRequest } from "@/shared/types/api"
import { useApiErrorMessage } from "@/shared/hooks/useApiErrorMessage"
import { assistantKeys, scopedOptions } from "../api/assistant"
import type { QuestionReceipt } from "../api/question"
import { QuestionDock } from "./QuestionDock"

interface RequestPage {
  items: Array<QuestionRequest & { task_title: string }>
  next_cursor: string | null
  receipts: QuestionReceipt[]
}

export function AssistantRequests({ renderQuestion }: { renderQuestion?: (request: QuestionRequest) => ReactNode }) {
  const { t } = useTranslation("chat")
  const userId = useAuthStore((state) => state.user?.id ?? "anonymous")
  const workspaceId = useWorkspaceStore((state) => state.currentId)
  const errorMessage = useApiErrorMessage()
  const requests = useInfiniteQuery({
    queryKey: [...assistantKeys.all(userId, workspaceId), "requests"],
    initialPageParam: null as string | null,
    queryFn: ({ pageParam, signal }) => http.get<RequestPage>(`/api/assistant/requests${pageParam
      ? `?cursor=${encodeURIComponent(pageParam)}` : ""}`, scopedOptions(workspaceId, signal)),
    getNextPageParam: (page) => page.next_cursor ?? undefined,
    refetchInterval: 5_000,
    enabled: userId !== "anonymous" && !!workspaceId,
  })
  const items = [...new Map(requests.data?.pages.flatMap((page) => page.items.map((item) => [item.id, item] as const))).values()]
  const receipts = requests.data?.pages[0]?.receipts ?? []
  if (!requests.error && !items.length && !receipts.length && !requests.hasNextPage) return null
  return <div className="border-hair flex-none border-b px-5 py-2">
    <details open={items.length > 0}>
      <summary className="text-n600 cursor-pointer text-sm">{t("assistant.requests.title", { count: items.length })}</summary>
      <div className="scr mx-auto max-h-[55vh] max-w-190 space-y-3 overflow-y-auto py-3" aria-label={t("assistant.requests.label")}>
        {requests.error ? <p role="alert" className="text-sm">{errorMessage(requests.error)}</p> : <>
          {items.map((request) => <section key={request.id} className="border-hair rounded-xl border p-3">
            <div className="mb-2 flex items-center justify-between gap-3 text-xs">
              <span>{request.task_title}</span>
              <Link className="underline" to={paths.chat(request.session_id)}>{t("assistant.requests.openTask")}</Link>
            </div>
            {renderQuestion ? renderQuestion(request) : <QuestionDock request={request} />}
          </section>)}
          {requests.hasNextPage && <button type="button" className="text-sm underline" disabled={requests.isFetchingNextPage}
            onClick={() => void requests.fetchNextPage()}>{t("assistant.requests.more")}</button>}
          {receipts.length > 0 && <details className="text-n600 text-xs">
            <summary className="cursor-pointer">{t("assistant.requests.receipts")}</summary>
            <ul className="mt-2 space-y-2">{receipts.map((receipt) => <li key={receipt.command_id}>
              <span>{t(`assistant.requests.${receipt.state === "applied" ? "applied" : receipt.state === "failed" ? "failed" : "accepted"}`)}</span>
              <span className="ml-2 break-all font-mono">{receipt.command_id}</span>
              {receipt.session_id && <Link className="ml-2 underline" to={paths.chat(receipt.session_id)}>{t("assistant.requests.openTask")}</Link>}
            </li>)}</ul>
          </details>}
        </>}
      </div>
    </details>
  </div>
}
