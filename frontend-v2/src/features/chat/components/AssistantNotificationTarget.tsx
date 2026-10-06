import { useTranslation } from "react-i18next"
import { useApiErrorMessage } from "@/shared/hooks/useApiErrorMessage"
import { useAssistantResultTarget } from "../api/assistant"
import { AssistantTaskCard } from "./AssistantTaskCard"

/** The notification's original result remains selected when newer runs finish. */
export function AssistantNotificationTarget({ taskId, resultId }: { taskId: string; resultId: string }) {
  const { t } = useTranslation("chat")
  const errorMessage = useApiErrorMessage()
  const query = useAssistantResultTarget(resultId)
  return <div className="border-hair scr max-h-72 flex-none overflow-y-auto border-b px-5 py-2" aria-label={t("assistant.notificationResult")}>
    <p className="text-n600 text-xs">{t("assistant.notificationResult")}</p>
    {query.error ? <p role="alert">{errorMessage(query.error)}</p>
      : query.data && query.data.task.task.id !== taskId ? <p role="alert">{t("assistant.notificationUnavailable")}</p>
        : query.isPending ? <p role="status">{t("assistant.loadingTask")}</p>
          : <AssistantTaskCard key={resultId} taskId={taskId} initial={query.data.task} selectedResult={query.data.result} />}
  </div>
}
