// "我已核对，继续" on a task card whose external outcomes are unverified. The
// owner checks the desktop and confirms; the server releases only outcomes
// nothing else can resolve and never resends them, and a paused task then
// resumes with the revision that confirmation left behind.
import { useRef, useState } from "react"
import { useTranslation } from "react-i18next"
import { ApiError } from "@/shared/api/http"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import { useApiErrorMessage } from "@/shared/hooks/useApiErrorMessage"
import { toast } from "@/shared/ui/Toast"
import { useAssistantControl, type AssistantTaskView } from "../api/assistant"
import { useAcknowledgeTaskEffects, type AssistantEffectsAcknowledgement } from "../api/assistant-effects"
import { pendingSendIdentity } from "../lib/pending-send"
import { useEffectAcknowledgeRequests } from "../stores/effect-acknowledge"

export function AssistantEffectAcknowledge({ value }: { value: AssistantTaskView }) {
  const { task } = value
  const requested = useEffectAcknowledgeRequests((state) => state.taskIds.has(task.id))
  if ((task.observed_state !== "effect_unknown" && !requested) || task.desired_state === "canceled" || task.archived_at) return null
  return <AcknowledgeAction task={task} />
}

function AcknowledgeAction({ task }: { task: AssistantTaskView["task"] }) {
  const { t } = useTranslation("chat")
  const clear = useEffectAcknowledgeRequests((state) => state.clear)
  const acknowledge = useAcknowledgeTaskEffects()
  const control = useAssistantControl()
  const errorMessage = useApiErrorMessage()
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
        text: JSON.stringify([task.id, task.control_revision]),
      })
      if (!current()) return
      let receipt: AssistantEffectsAcknowledgement
      try {
        receipt = await acknowledge.mutateAsync({ taskId: task.id, revision: task.control_revision, key: identity.id })
        identity.confirmed()
      } catch (error) {
        if (error instanceof ApiError && error.status < 500) identity.confirmed()
        toast("error", errorMessage(error))
        return
      }
      clear(task.id)
      if (receipt.remaining_unresolved) {
        toast("info", t("assistant.card.effectAck.pending"))
        return
      }
      if (receipt.desired_state !== "paused") {
        toast("info", t("assistant.card.effectAck.done"))
        return
      }
      const resume = await pendingSendIdentity(JSON.stringify([user, workspace, "task-control"]), {
        text: JSON.stringify([task.id, "resume", receipt.task_revision, null]),
      })
      if (!current()) return
      try {
        await control.mutateAsync({ taskId: task.id, action: "resume", revision: receipt.task_revision, run: null, key: resume.id })
        resume.confirmed()
        toast("info", t("assistant.card.controlDone.resume"))
      } catch (error) {
        if (error instanceof ApiError && error.status < 500) resume.confirmed()
        toast("error", errorMessage(error))
      }
    } catch (error) { toast("error", errorMessage(error)) }
    finally { sending.current = false; setBusy(false) }
  }

  return (
    <div className="mt-3 ps-5">
      <p className="text-n700 text-sm leading-relaxed">{t("assistant.card.effectAck.hint")}</p>
      <button type="button" disabled={busy || acknowledge.isPending || control.isPending} onClick={() => void confirm()}
        className="border-hair text-n800 hover:bg-hairsoft mt-2 inline-flex items-center rounded-full border px-3 py-1 text-sm font-medium disabled:opacity-50">
        {t("assistant.card.effectAck.action")}
      </button>
    </div>
  )
}
