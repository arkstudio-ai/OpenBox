import { useTranslation } from "react-i18next"
import { useApiErrorMessage } from "@/shared/hooks/useApiErrorMessage"
import { useAssistantResultTarget } from "../api/assistant"
import { AssistantTaskCard } from "./AssistantTaskCard"

/** The notification's original result remains selected when newer runs finish. */
export function AssistantNotificationTarget({ taskId, resultId }: { taskId: string; resultId: string }) {
  const { t } = useTranslation("chat")
  const errorMessage = useApiErrorMessage()
  const query = useAssistantResultTarget(resultId)
  return <div className="scr mx-auto max-h-80 w-full max-w-190 flex-none overflow-y-auto px-3 pt-1 sm:px-6.5"
    aria-label={t("assistant.notificationResult")}>
    <p className="text-n600 text-xs">{t("assistant.notificationResult")}</p>
    {query.error ? <p role="alert" className="text-dangerink text-sm">{errorMessage(query.error)}</p>
      : query.data && query.data.task.task.id !== taskId ? <p role="alert" className="text-n700 text-sm">{t("assistant.notificationUnavailable")}</p>
        : query.isPending ? <p role="status" className="text-n600 text-sm">{t("assistant.card.loading")}</p>
          : <AssistantTaskCard key={resultId} taskId={taskId} initial={query.data.task} selectedResult={query.data.result} />}
  </div>
}
