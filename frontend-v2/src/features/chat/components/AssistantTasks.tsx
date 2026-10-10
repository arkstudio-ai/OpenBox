// "我的任务": everything handed to the personal assistant, in one drawer opened
// from the top bar. Waiting-on-you first, then work in progress, then what
// finished lately. Following an existing conversation lives at its foot.
import { useEffect, useState, type ReactNode } from "react"
import { ListChecks, Plus } from "lucide-react"
import { useTranslation } from "react-i18next"
import { cn } from "@/shared/lib/cn"
import { Dialog, DialogTitle } from "@/shared/ui/Dialog"
import { Sheet } from "@/shared/ui/Sheet"
import { Spinner } from "@/shared/ui/Spinner"
import { useApiErrorMessage } from "@/shared/hooks/useApiErrorMessage"
import { useAssistantWatch, type AssistantWatchItem } from "../api/assistant-watch"
import { useAssistantRequests } from "../api/assistant-requests"
import { useAssistantTasksPanel } from "../stores/assistant-tasks-panel"
import type { QuestionRequest } from "@/shared/types/api"
import { isActiveTask, taskStatus, type TaskStatus } from "../lib/task-status"
import { AssistantTaskCard } from "./AssistantTaskCard"
import { AssistantLinkExisting } from "./AssistantLinkExisting"
import { AssistantRequests } from "./AssistantRequests"

function statusOf(item: AssistantWatchItem): TaskStatus {
  return taskStatus({ sessionStatus: item.session_status, observedState: item.observed_state,
    desiredState: item.desired_state, pendingQuestions: item.pending_questions,
    outcome: item.latest_result?.outcome })
}

const GROUPS = [
  { key: "waiting", match: (status: TaskStatus) => status === "waiting" },
  { key: "active", match: (status: TaskStatus) => isActiveTask(status) && status !== "waiting" },
  { key: "finished", match: (status: TaskStatus) => !isActiveTask(status) },
] as const

/** The top-bar entry: unfinished work and a red count of requests awaiting the user. */
export function AssistantTopbarActions({ renderQuestion }: { renderQuestion?: (request: QuestionRequest) => ReactNode }) {
  const { t } = useTranslation("chat")
  const { open, setOpen } = useAssistantTasksPanel()
  const pending = useAssistantRequests()
  useEffect(() => () => setOpen(false), [pending.scopeKey, setOpen])
  const items = useAssistantWatch().data?.items ?? []
  const statuses = items.map(statusOf)
  const active = statuses.filter(isActiveTask).length
  const waiting = pending.count + items.filter((item) => statusOf(item) === "waiting" && !pending.sessions.has(item.session_id))
    .reduce((total, item) => total + Math.max(item.pending_questions, 1), 0)
  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        aria-label={waiting > 0 ? t("assistant.taskList.buttonWaiting", { count: waiting }) : t("assistant.taskList.button")}
        className="border-hair text-n800 hover:bg-hairsoft relative flex h-8 flex-none items-center gap-1.5 rounded-full border px-3 text-sm"
      >
        <ListChecks size={15} strokeWidth={2.2} aria-hidden />
        <span className="hidden sm:inline">{t("assistant.taskList.title")}</span>
        {active > 0 && <span className="bg-hairsoft text-n800 rounded-full px-1.5 text-xs leading-5">{active}</span>}
        {waiting > 0 && <span className="text-dangerink whitespace-nowrap text-xs font-medium">
          {t("assistant.taskList.pendingCount", { count: waiting })}{pending.hasMore ? "+" : ""}
        </span>}
      </button>
      <AssistantTasksSheet open={open} onClose={() => setOpen(false)} renderQuestion={renderQuestion} />
    </>
  )
}

export function AssistantTasksSheet({ open, onClose, renderQuestion }: {
  open: boolean; onClose: () => void; renderQuestion?: (request: QuestionRequest) => ReactNode
}) {
  const { t } = useTranslation("chat")
  const watch = useAssistantWatch(open)
  const errorMessage = useApiErrorMessage()
  const [following, setFollowing] = useState(false)
  const items = watch.data?.items ?? []
  const pending = useAssistantRequests(open)
  return (
    <>
      <Sheet
        open={open}
        onClose={onClose}
        title={t("assistant.taskList.title")}
        description={t("assistant.taskList.description")}
        closeLabel={t("assistant.taskList.close")}
        footer={
          <button type="button" onClick={() => setFollowing(true)}
            className="text-n800 hover:bg-hairsoft -mx-2 flex items-center gap-2 rounded-full px-2 py-1.5 text-sm">
            <Plus size={15} strokeWidth={2.2} aria-hidden />
            {t("assistant.taskList.followExisting")}
          </button>
        }
      >
        <div className="mb-4">
          <AssistantRequests renderQuestion={renderQuestion} onNavigate={onClose} />
        </div>
        {watch.error ? <p role="alert" className="text-dangerink text-sm">{errorMessage(watch.error)}</p>
          : watch.isPending ? <div className="flex justify-center py-10"><Spinner className="size-5" /></div>
            : items.length === 0 ? (pending.count > 0 ? null : (
              <div className="py-10 text-center">
                <p className="text-ink text-base">{t("assistant.taskList.emptyTitle")}</p>
                <p className="text-n600 mx-auto mt-2 max-w-80 text-sm leading-relaxed">{t("assistant.taskList.emptyHint")}</p>
              </div>
            )) : (
              <div className="space-y-5">
                {GROUPS.map((group) => {
                  const rows = items.filter((item) => group.match(statusOf(item)))
                  if (rows.length === 0) return null
                  return (
                    <section key={group.key} aria-label={t(`assistant.taskList.groups.${group.key}`)}>
                      <h3 className={cn("mb-1 text-sm font-medium", group.key === "waiting" ? "text-dangerink" : "text-n600")}>
                        {t(`assistant.taskList.groups.${group.key}`)} · {rows.length}
                      </h3>
                      {rows.map((item) => <AssistantTaskCard key={item.task_id} taskId={item.task_id} />)}
                    </section>
                  )
                })}
                {watch.data?.has_more && <p className="text-n600 text-sm leading-relaxed">{t("assistant.taskList.more")}</p>}
              </div>
            )}
      </Sheet>
      <Dialog open={following} onClose={() => setFollowing(false)} wide label={t("assistant.link.title")}>
        <DialogTitle>{t("assistant.link.title")}</DialogTitle>
        <AssistantLinkExisting />
      </Dialog>
    </>
  )
}
