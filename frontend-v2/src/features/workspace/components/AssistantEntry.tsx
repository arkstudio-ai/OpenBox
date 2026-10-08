import { ChevronRight, Sparkles } from "lucide-react"
import { useTranslation } from "react-i18next"
import { useMatch, useNavigate } from "react-router"
import { cn } from "@/shared/lib/cn"
import { paths } from "@/shared/router/paths"

interface AssistantEntryProps {
  /** Unread updates; hidden at zero, capped at 99+. */
  unread?: { count: number; lowerBound: boolean }
}

/** The personal assistant's card at the top of the sidebar. It is where work is handed over and
 *  followed up, so it stands apart from the centre pages instead of being one more row among them:
 *  a raised card in the sidebar's own neutral colours (card ground, hairline, the new-chat row's
 *  chip), never the accent, which was a second palette next to everything else (2026-10-08). */
export function AssistantEntry({ unread }: AssistantEntryProps) {
  const { t } = useTranslation("workspace")
  const navigate = useNavigate()
  const active = useMatch(paths.assistant) !== null
  const count = unread?.count ?? 0
  return (
    <button
      type="button"
      onClick={() => navigate(paths.assistant)}
      aria-current={active ? "page" : undefined}
      title={t("assistantHint")}
      className={cn(
        "group flex flex-none items-center gap-2.5 rounded-xl border px-2 py-2 text-start transition-colors",
        "[@media(max-height:760px)]:py-1",
        active ? "border-n300 bg-n200" : "border-hair bg-card hover:border-n300",
      )}
    >
      <span
        className={cn(
          "bg-a200 text-n800 flex size-8 flex-none items-center justify-center rounded-full",
          "transition-transform duration-150 group-hover:scale-105 [@media(max-height:760px)]:size-7",
        )}
      >
        <Sparkles size={16} strokeWidth={2.2} aria-hidden />
      </span>
      <span className="min-w-0 flex-1">
        <span className="text-ink block truncate text-base leading-5 font-medium">{t("assistant")}</span>
        <span className="text-n600 block truncate text-xs leading-4 [@media(max-height:760px)]:hidden">
          {t("assistantTagline")}
        </span>
      </span>
      {count > 0 ? (
        <span
          data-testid="assistant-unread"
          className="bg-accent text-bg me-1 flex-none rounded-full px-1.5 py-0.5 text-xs leading-none font-semibold"
        >
          {count > 99 ? "99+" : unread?.lowerBound ? `${count}+` : count}
        </span>
      ) : (
        <ChevronRight size={15} strokeWidth={2.2} className="text-n500 me-0.5 flex-none" aria-hidden />
      )}
    </button>
  )
}
