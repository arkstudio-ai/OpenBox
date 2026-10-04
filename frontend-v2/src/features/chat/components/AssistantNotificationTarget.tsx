import { useTranslation } from "react-i18next"
import { useAssistantResultTarget } from "../api/assistant"
import { AssistantTaskCard } from "./AssistantTaskCard"

/** The notification's original result remains selected when newer runs finish. */
export function AssistantNotificationTarget({ taskId, resultId }: { taskId: string; resultId: string }) {
  const { t } = useTranslation("chat")
  const query = useAssistantResultTarget(resultId)
  return <div className="border-hair scr max-h-72 flex-none overflow-y-auto border-b px-5 py-2" aria-label={t("assistant.notificationResult")}>
    <p className="text-n600 text-xs">{t("assistant.notificationResult")}</p>
    {query.error || query.data && query.data.task.task.id !== taskId
      ? <p role="alert">{t("assistant.sourceUnavailable")}</p>
      : query.isPending || !query.isFetchedAfterMount && query.isFetching ? <p role="status">{t("assistant.loadingTask")}</p>
        : query.data && <AssistantTaskCard key={resultId} taskId={taskId} initial={query.data.task} selectedResult={query.data.result} />}
  </div>
}
