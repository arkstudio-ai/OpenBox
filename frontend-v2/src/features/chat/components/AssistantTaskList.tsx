import { useState } from "react"
import { useTranslation } from "react-i18next"
import { useApiErrorMessage } from "@/shared/hooks/useApiErrorMessage"
import { useAssistantTaskPages } from "../api/assistant"
import { AssistantTaskCard } from "./AssistantTaskCard"

export function AssistantTaskList() {
  const { t } = useTranslation("chat")
  const [open, setOpen] = useState(false)
  const tasks = useAssistantTaskPages(open)
  const errorMessage = useApiErrorMessage()
  const items = Array.from(new Map(tasks.data?.pages.flatMap((page) => page.tasks.map((item) => [item.task.id, item] as const))).values())
  return <div className="border-hair flex-none border-b px-5 py-2">
    <button type="button" className="text-n600 text-sm underline" aria-expanded={open}
      onClick={() => setOpen(!open)}>{t("assistant.tasks")}</button>
    {open && <div className="scr mx-auto max-h-64 max-w-190 overflow-y-auto" aria-label={t("assistant.tasks")}>
      {tasks.error ? <p role="alert" className="py-3 text-sm">{errorMessage(tasks.error)}</p>
        : tasks.isPending ? <p role="status" className="py-3 text-sm">{t("assistant.loadingTask")}</p>
          : <>{items.map((item) => <AssistantTaskCard key={item.task.id} taskId={item.task.id} initial={item} />)}
            {items.length === 0 && <p className="text-n600 py-3 text-sm">{t("assistant.noTasks")}</p>}
            {tasks.hasNextPage && <button type="button" className="py-3 text-sm underline" disabled={tasks.isFetchingNextPage}
              onClick={() => void tasks.fetchNextPage()}>{t("assistant.moreTasks")}</button>}
          </>}
    </div>}
  </div>
}
