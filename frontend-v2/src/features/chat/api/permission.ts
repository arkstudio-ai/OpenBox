import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useTranslation } from "react-i18next"
import { ApiError, http } from "@/shared/api/http"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import { toast } from "@/shared/ui/Toast"
import type { PermissionRequest } from "@/shared/types/api"
import { usePendingStore } from "../stores/pending"
import { chatKeys } from "./keys"
import { useUserId } from "./messages"
import { pendingSendIdentity } from "../lib/pending-send"
import type { QuestionReceipt } from "./question"

export type PermissionAction = "allow" | "allow_always" | "reject"

const permissionReplyAction = { allow: "once", allow_always: "always", reject: "reject" } as const
type ReplyVars = { requestId: string; action: PermissionAction; assistant?: PermissionRequest["assistant"] }

export async function postPermissionReply(userId: string, { requestId, action, assistant }: ReplyVars): Promise<QuestionReceipt> {
  const endpoint = `/api/agent/permission/${requestId}`
  const body = { action: permissionReplyAction[action] }
  if (!assistant) return http.post<QuestionReceipt>(endpoint, body)
  const current = () => useAuthStore.getState().user?.id === userId
    && useWorkspaceStore.getState().currentId === assistant.workspace_id
  if (!current()) throw new Error("Permission account or workspace changed")
  const payload = { ...body, expected_request_revision: assistant.request_revision,
    options_hash: assistant.options_hash, source_ref: { kind: "card" } }
  const identity = await pendingSendIdentity(JSON.stringify([userId, assistant.workspace_id,
    assistant.assistant_session_id, "permission-reply"]), { text: JSON.stringify([requestId, payload]) })
  if (!current()) throw new Error("Permission account or workspace changed")
  try {
    const receipt = await http.post<QuestionReceipt>(endpoint, { ...payload, reply_id: identity.id },
      { headers: { "X-Workspace-Id": assistant.workspace_id } })
    identity.confirmed()
    return receipt
  } catch (error) {
    if (error instanceof ApiError && error.status < 500) identity.confirmed()
    throw error
  }
}

export function usePermissionsQuery() {
  const userId = useUserId()
  return useQuery({
    queryKey: chatKeys.permissions(userId),
    queryFn: () => http.get<PermissionRequest[]>("/api/agent/permission"),
  })
}

export function useReplyPermission() {
  const userId = useUserId()
  const qc = useQueryClient()
  const { t } = useTranslation("chat")
  const current = (assistant: ReplyVars["assistant"]) => !assistant || (useAuthStore.getState().user?.id === userId
    && useWorkspaceStore.getState().currentId === assistant.workspace_id)
  const refresh = () => {
    void qc.invalidateQueries({ queryKey: chatKeys.permissions(userId) })
    void qc.invalidateQueries({ queryKey: ["assistant", userId] })
  }
  return useMutation({
    mutationFn: (value: ReplyVars) => postPermissionReply(userId, value),
    onSuccess: (receipt, { requestId, assistant }) => {
      if (!current(assistant)) return
      usePendingStore.getState().removePermission(requestId)
      refresh()
      if (assistant) toast(receipt.state === "failed" ? "error" : "info",
        t(`assistant.requests.${receipt.state ?? "accepted"}`))
    },
    onError: (error, { requestId, assistant }) => {
      if (!current(assistant)) return
      if (error instanceof ApiError && [404, 410].includes(error.status)) {
        usePendingStore.getState().removePermission(requestId)
      }
      refresh()
    },
  })
}
