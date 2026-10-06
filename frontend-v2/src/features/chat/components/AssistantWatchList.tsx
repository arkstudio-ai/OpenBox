// The conversations the personal assistant watches, listed under the
// sidebar's "个人助理" row. The layout injects this into the workspace sidebar
// (features never import each other, ENGINEERING_SPEC §4). Hidden when empty.
import { useTranslation } from "react-i18next"
import { Link, useMatch } from "react-router"
import { cn } from "@/shared/lib/cn"
import { paths, routePatterns } from "@/shared/router/paths"
import { useAssistantWatch, type AssistantWatchItem } from "../api/assistant-watch"
import { WATCH_DOT, watchState } from "../lib/watch-state"

/** Enough to reach for; the assistant page lists the rest. */
const MAX_ROWS = 8

function WatchRow({ item }: { item: AssistantWatchItem }) {
  const { t } = useTranslation("chat")
  const active = useMatch(`${paths.app}/${routePatterns.chat}`)?.params.sessionId === item.session_id
  const state = watchState(item)
  const pending = Math.max(0, item.pending_questions)
  return (
    <Link
      to={paths.chat(item.session_id)}
      title={item.project.name}
      aria-current={active ? "page" : undefined}
      className={cn(
        "text-md text-ink flex min-h-8 items-center gap-2 rounded-full py-1 ps-9 pe-2",
        active ? "bg-n200 font-medium" : "hover:bg-hairsoft",
      )}
    >
      <span role="img" aria-label={t(`assistant.watch.state.${state}`)} data-state={state}
        className={cn("size-1.5 flex-none rounded-full", WATCH_DOT[state])} />
      <span className="min-w-0 flex-1 truncate">{item.title}</span>
      {pending > 0 && (
        <span aria-label={t("assistant.watch.pending", { count: pending })}
          className="bg-danger text-bg flex-none rounded-full px-1.5 py-0.5 text-xs leading-none font-semibold">
          {pending > 99 ? "99+" : pending}
        </span>
      )}
    </Link>
  )
}

export function AssistantWatchList() {
  const { t } = useTranslation("chat")
  const watch = useAssistantWatch()
  const items = watch.data?.items ?? []
  if (items.length === 0) return null
  const more = items.length > MAX_ROWS || watch.data?.has_more === true
  return (
    <div role="group" aria-label={t("assistant.watch.title")} className="flex flex-col gap-px" data-testid="assistant-watch-list">
      {items.slice(0, MAX_ROWS).map((item) => (
        <WatchRow key={item.task_id} item={item} />
      ))}
      {more && (
        <Link to={paths.assistant} className="text-n600 hover:text-ink py-1 ps-9 text-xs underline-offset-2 hover:underline">
          {t("assistant.watch.more")}
        </Link>
      )}
    </div>
  )
}
