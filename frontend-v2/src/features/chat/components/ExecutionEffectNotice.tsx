// "我已核对，继续" on the execution page itself.
//
// A task stopped mid-step leaves an unverified external outcome; until the
// owner confirms it, every message sent here is refused with
// ASSISTANT_EFFECT_UNRESOLVED. The only control used to be a button on the
// task card in the assistant conversation, so people on this page were sent
// to look for a card that is not here (2026-10-31). This notice sits above the
// composer whenever the session's task reports effect_unknown, or a send from
// this tab was just refused for it, and confirms with the same request the
// card uses. Nothing is resent; the person sends their message again.
import { useRef, useState } from "react"
import { useTranslation } from "react-i18next"
import { useQueryClient } from "@tanstack/react-query"
import { AlertCircle } from "lucide-react"
import { ApiError } from "@/shared/api/http"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import { useApiErrorMessage } from "@/shared/hooks/useApiErrorMessage"
import { toast } from "@/shared/ui/Toast"
import type { Session } from "@/shared/types/api"
import { useAcknowledgeTaskEffects } from "../api/assistant-effects"
import { pendingSendIdentity } from "../lib/pending-send"
import { useEffectAcknowledgeRequests } from "../stores/effect-acknowledge"

export function ExecutionEffectNotice({ sessionId, session }: { sessionId: string; session?: Session }) {
  const requested = useEffectAcknowledgeRequests((state) => state.sessionIds.has(sessionId))
  const control = session?.task_control
  if (!session?.assistant_managed || session.kind === "assistant" || !control) return null
  if (control.observed_state !== "effect_unknown" && !requested) return null
  return <Notice sessionId={sessionId} taskId={control.task_id} revision={control.expected_revision} />
}

function Notice({ sessionId, taskId, revision }: { sessionId: string; taskId: string; revision: number }) {
  const { t } = useTranslation("chat")
  const clearSession = useEffectAcknowledgeRequests((state) => state.clearSession)
  const acknowledge = useAcknowledgeTaskEffects()
  const errorMessage = useApiErrorMessage()
  const qc = useQueryClient()
  const sending = useRef(false)
  const [busy, setBusy] = useState(false)

  const confirm = async () => {
    if (sending.current) return
    sending.current = true
    setBusy(true)
    const user = useAuthStore.getState().user?.id
    const workspace = useWorkspaceStore.getState().currentId
    const current = () => user === useAuthStore.getState().user?.id && workspace === useWorkspaceStore.getState().currentId
    try {
      const identity = await pendingSendIdentity(JSON.stringify([user, workspace, "task-effects-ack"]), {
        text: JSON.stringify([taskId, revision]),
      })
      if (!current()) return
      try {
        const receipt = await acknowledge.mutateAsync({ taskId, revision, key: identity.id })
        identity.confirmed()
        clearSession(sessionId)
        void qc.invalidateQueries({ queryKey: ["session", user, sessionId] })
        toast("info", t(receipt.remaining_unresolved ? "execution.effectAck.pending" : "execution.effectAck.done"))
      } catch (error) {
        if (error instanceof ApiError && error.status < 500) identity.confirmed()
        toast("error", errorMessage(error))
      }
    } catch (error) { toast("error", errorMessage(error)) }
    finally { sending.current = false; setBusy(false) }
  }

  return (
    <div role="status" className="mx-auto flex w-full max-w-170 items-start gap-2 px-1 pb-1.5">
      <AlertCircle size={13} className="mt-0.5 flex-none text-danger" aria-hidden />
      <div className="min-w-0 flex-1">
        <p className="text-xs font-medium leading-5">{t("execution.effectAck.title")}</p>
        <p className="text-n700 text-xs leading-5">{t("execution.effectAck.body")}</p>
        <button type="button" disabled={busy || acknowledge.isPending} onClick={() => void confirm()}
          className="border-hair text-n800 hover:bg-hairsoft mt-1.5 inline-flex items-center rounded-full border px-3 py-1 text-xs font-medium disabled:opacity-50">
          {t("execution.effectAck.action")}
        </button>
      </div>
    </div>
  )
}

/** Stopping a task-linked conversation cancels the whole task: ask once, inline, before doing it. */
export function StopTaskNotice({ open, pending, onConfirm, onKeep }: { open: boolean; pending: boolean; onConfirm: () => void; onKeep: () => void }) {
  const { t } = useTranslation("chat")
  if (!open) return null
  return (
    <div role="alertdialog" aria-label={t("execution.stopTask.title")}
      className="mx-auto flex w-full max-w-170 items-start gap-2 px-1 pb-1.5">
      <AlertCircle size={13} className="mt-0.5 flex-none text-danger" aria-hidden />
      <div className="min-w-0 flex-1">
        <p className="text-xs font-medium leading-5">{t("execution.stopTask.title")}</p>
        <p className="text-n700 text-xs leading-5">{t("execution.stopTask.body")}</p>
        <div className="mt-1.5 flex flex-wrap gap-2">
          <button type="button" disabled={pending} onClick={onConfirm}
            className="bg-danger text-bg inline-flex items-center rounded-full px-3 py-1 text-xs font-medium disabled:opacity-50">
            {t("execution.stopTask.confirm")}
          </button>
          <button type="button" disabled={pending} onClick={onKeep}
            className="border-hair text-n800 hover:bg-hairsoft inline-flex items-center rounded-full border px-3 py-1 text-xs font-medium disabled:opacity-50">
            {t("execution.stopTask.keep")}
          </button>
        </div>
      </div>
    </div>
  )
}
