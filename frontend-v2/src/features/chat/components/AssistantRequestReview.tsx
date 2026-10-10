import { useEffect, useRef, useState } from "react"
import { useMutation, useQuery } from "@tanstack/react-query"
import { useTranslation } from "react-i18next"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import { http } from "@/shared/api/http"
import type { AssistantRequestBinding } from "@/shared/types/api"
import { useApiErrorMessage } from "@/shared/hooks/useApiErrorMessage"
import { assistantKeys, scopedOptions } from "../api/assistant"

interface Review { segments: string[]; display_token: string; request_revision: string }

/** A read/fetch is not display evidence. Every block must enter the visible
 * foreground viewport before the next human message can use this context. */
export function AssistantRequestReview({ requestId, binding }: { requestId: string; binding: AssistantRequestBinding }) {
  const { t } = useTranslation("chat")
  const [open, setOpen] = useState(false)
  const userId = useAuthStore((s) => s.user?.id ?? "anonymous")
  const workspaceId = useWorkspaceStore((s) => s.currentId)
  const errorMessage = useApiErrorMessage()
  const root = useRef<HTMLDivElement>(null)
  const current = () => userId !== "anonymous" && useAuthStore.getState().user?.id === userId
    && useWorkspaceStore.getState().currentId === binding.workspace_id && workspaceId === binding.workspace_id
  const query = useQuery({
    queryKey: [...assistantKeys.all(userId, workspaceId), "request-review", binding.kind, requestId, binding.request_revision],
    queryFn: ({ signal }) => http.get<Review>(`/api/assistant/requests/${binding.kind}/${encodeURIComponent(requestId)}/review`,
      scopedOptions(workspaceId, signal)),
    enabled: open && current(), retry: false, staleTime: 0,
  })
  const display = useMutation({
    mutationFn: (shown: { token: string; userId: string; workspaceId: string }) => {
      if (shown.userId === "anonymous" || useAuthStore.getState().user?.id !== shown.userId
          || useWorkspaceStore.getState().currentId !== shown.workspaceId) {
        throw new Error("Request account or workspace changed")
      }
      return http.post("/api/assistant/requests/displayed", { display_token: shown.token }, scopedOptions(shown.workspaceId))
    },
  })
  const token = query.data?.display_token
  const segments = query.data?.segments
  const revision = query.data?.request_revision
  const { mutate } = display
  useEffect(() => {
    if (!open || !token || revision !== binding.request_revision || !root.current || !segments?.length) return
    const nodes = Array.from(root.current.children)
    const seen = new Set<Element>()
    let sent = false
    let active = true
    const observer = new IntersectionObserver((entries) => {
      if (!active || document.visibilityState !== "visible") return
      for (const entry of entries) {
        if (entry.isIntersecting && entry.intersectionRatio >= 0.999) seen.add(entry.target)
      }
      if (!sent && seen.size === nodes.length) {
        sent = true
        mutate({ token, userId, workspaceId: binding.workspace_id })
      }
    }, { threshold: [1] })
    const observe = () => {
      observer.disconnect()
      if (document.visibilityState === "visible") nodes.forEach((node) => observer.observe(node))
    }
    observe()
    document.addEventListener("visibilitychange", observe)
    return () => { active = false; observer.disconnect(); document.removeEventListener("visibilitychange", observe) }
  }, [open, token, revision, binding.request_revision, binding.workspace_id, userId, segments, mutate])
  const error = query.error ?? display.error
  return <details className="mt-2 text-xs" onToggle={(e) => setOpen(e.currentTarget.open)}>
    <summary className="cursor-pointer">{t("assistant.requests.review")}</summary>
    <p className="my-2 text-n600">{t("assistant.requests.reviewHint")}</p>
    {error ? <p role="alert">{errorMessage(error)}</p> : <div ref={root}>
      {segments?.map((segment, index) => <p key={index} className="whitespace-pre-wrap break-all font-mono">{segment}</p>)}
    </div>}
    {display.isSuccess && display.variables.token === token && <p className="my-2">{t("assistant.requests.reviewed")}</p>}
  </details>
}
