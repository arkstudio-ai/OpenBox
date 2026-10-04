import { useRef, useState } from "react"
import { useTranslation } from "react-i18next"
import { ApiError } from "@/shared/api/http"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import { useApiErrorMessage } from "@/shared/hooks/useApiErrorMessage"
import { toast } from "@/shared/ui/Toast"
import { useAssistantControl, type AssistantControlAction, type AssistantTaskView } from "../api/assistant"
import { pendingSendIdentity } from "../lib/pending-send"

export function AssistantTaskControls({ value }: { value: AssistantTaskView }) {
  const { t } = useTranslation("chat")
  const control = useAssistantControl()
  const errorMessage = useApiErrorMessage()
  const sending = useRef(false)
  const [preparing, setPreparing] = useState(false)
  const [receipt, setReceipt] = useState<{ taskId: string; basedOn: number; id: string } | null>(null)
  const { task, run_binding: binding } = value
  const label = (() => {
    switch (task.observed_state) {
      case "idle": return t("assistant.task.state.idle")
      case "paused": return t("assistant.task.state.paused")
      case "pausing": return t("assistant.task.state.pausing")
      case "resuming": return t("assistant.task.state.resuming")
      case "resume_blocked": return t("assistant.task.state.resumeBlocked")
      case "canceled": return t("assistant.task.state.canceled")
      case "canceling": return t("assistant.task.state.canceling")
      case "effect_unknown": return t("assistant.task.state.effectUnknown")
      case "running": return t("assistant.task.state.running")
      case "queued": return t("assistant.task.state.queued")
      case "waiting_input": return t("assistant.task.state.waitingInput")
      case "completed": return t("assistant.task.state.completed")
      case "error": return t("assistant.task.state.error")
      case "aborted": return t("assistant.task.state.aborted")
      case "input_not_applied": return t("assistant.steerNotApplied")
      default: return t("assistant.task.state.unknown")
    }
  })()
  const submit = async (action: AssistantControlAction) => {
    if (sending.current) return
    sending.current = true
    setPreparing(true)
    const user = useAuthStore.getState().user?.id
    const workspace = useWorkspaceStore.getState().currentId
    const run = binding?.run_id && binding.phase !== "idle"
      ? { run_id: binding.run_id, generation: binding.generation } : null
    try {
      const identity = await pendingSendIdentity(JSON.stringify([user, workspace, "task-control"]), {
        text: JSON.stringify([task.id, action, task.control_revision, run]),
      })
      if (user !== useAuthStore.getState().user?.id || workspace !== useWorkspaceStore.getState().currentId) return
      try {
        const accepted = await control.mutateAsync({ taskId: task.id, action, revision: task.control_revision, run, key: identity.id })
        identity.confirmed()
        setReceipt({ taskId: task.id, basedOn: task.control_revision, id: accepted.command_id })
      } catch (error) {
        if (error instanceof ApiError && error.status < 500) identity.confirmed()
        if (error instanceof ApiError && error.code === "ASSISTANT_EFFECT_UNRESOLVED") toast("info", t("assistant.task.checkEffects"))
        else if (error instanceof ApiError && error.status === 409) toast("info", t("assistant.task.changed"))
        else toast("error", errorMessage(error))
      }
    } catch (error) { toast("error", errorMessage(error)) }
    finally { sending.current = false; setPreparing(false) }
  }
  const disabled = preparing || control.isPending
  const serverRevision = value.latest_control?.receipt.task_revision
  const receiptId = receipt?.taskId === task.id && (typeof serverRevision !== "number" || serverRevision <= receipt.basedOn)
    ? receipt.id : value.latest_control?.command_id
  const buttonClass = "border-hair rounded-lg border px-3 py-1.5 text-xs hover:bg-n200 disabled:cursor-not-allowed disabled:opacity-50"
  return <div className="mt-3 space-y-2">
    <p className="text-n600 text-xs" aria-live="polite">{t("assistant.task.currentState")} {label}</p>
    {task.observed_state === "effect_unknown" && <p className="text-n600 text-xs">{t("assistant.task.checkEffects")}</p>}
    {task.desired_state !== "canceled" && <div className="flex flex-wrap gap-2">
      {task.desired_state === "running" ? <button type="button" className={buttonClass} disabled={disabled}
        onClick={() => void submit("pause")}>{t("assistant.task.control.pause")}</button>
        : <button type="button" className={buttonClass} disabled={disabled || !["paused", "resume_blocked"].includes(task.observed_state)}
          onClick={() => void submit("resume")}>{t("assistant.task.control.resume")}</button>}
      <button type="button" className={buttonClass} disabled={disabled}
        onClick={() => void submit("cancel")}>{t("assistant.task.control.cancel")}</button>
    </div>}
    {preparing && <p role="status" className="text-n600 text-xs">{t("assistant.task.requesting")}</p>}
    {receiptId && <details className="text-n600 text-xs"><summary className="cursor-pointer">{t("assistant.task.controlReceipt")}</summary>
      <p className="break-all font-mono">{receiptId}</p></details>}
  </div>
}
