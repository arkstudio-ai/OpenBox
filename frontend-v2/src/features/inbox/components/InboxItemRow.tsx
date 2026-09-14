// One inbox row: category icon, title, one-line body, relative time, unread
// dot, workspace chip when the row belongs to another workspace.
import { useTranslation } from "react-i18next"
import {
  AlertCircle,
  Bell,
  CheckCircle2,
  HelpCircle,
  KeyRound,
  Megaphone,
  MessageSquare,
  Send,
} from "lucide-react"
import { cn } from "@/shared/lib/cn"
import { formatRelative } from "@/shared/lib/format"
import type { ReactNode } from "react"
import type { InboxItem } from "../api"

export function iconFor(item: InboxItem): ReactNode {
  const size = 16
  switch (item.kind) {
    case "task_completed":
    case "cron_completed":
      return <CheckCircle2 size={size} />
    case "task_failed":
    case "cron_failed":
    case "publish_failed":
      return <AlertCircle size={size} />
    case "input_required":
    case "approval_required":
      return <HelpCircle size={size} />
    case "publish_done":
      return <Send size={size} />
    case "platform_auth_expired":
    case "desktop_login_expired":
    case "desktop_login_reset":
      return <KeyRound size={size} />
    case "announcement":
      return <Megaphone size={size} />
    default:
      return item.category === "session" ? (
        <MessageSquare size={size} />
      ) : item.category === "notice" ? (
        <Megaphone size={size} />
      ) : (
        <Bell size={size} />
      )
  }
}

interface Props {
  item: InboxItem
  /** Set only when the row's workspace differs from the current one. */
  workspaceName?: string | null
  onOpen: (item: InboxItem) => void
}

export function InboxItemRow({ item, workspaceName, onOpen }: Props) {
  const { t } = useTranslation("inbox")
  const unread = item.readAt === null
  const failed = item.kind.endsWith("_failed")
  return (
    <button
      type="button"
      data-testid={`inbox-${item.id}`}
      onClick={() => onOpen(item)}
      className="hover:bg-hairsoft flex w-full items-start gap-3 rounded-xl px-3 py-3 text-start"
    >
      <span
        className={cn(
          "flex size-8.5 flex-none items-center justify-center rounded-full",
          unread ? "bg-a100 text-a800" : "bg-n200 text-n700",
          failed && "text-danger",
        )}
      >
        {iconFor(item)}
      </span>
      <span className="flex min-w-0 flex-1 flex-col gap-0.5">
        <span className="flex items-center gap-2">
          <span
            className={cn(
              "text-ink min-w-0 flex-1 truncate text-base",
              unread ? "font-semibold" : "font-medium",
            )}
          >
            {item.title}
          </span>
          <span className="text-n500 flex-none text-xs">{formatRelative(item.createdAt)}</span>
          {unread && (
            <span
              aria-label={t("tabs.all")}
              data-testid={`inbox-unread-${item.id}`}
              className="bg-accent size-1.75 flex-none rounded-full"
            />
          )}
        </span>
        {item.body && <span className="text-n700 line-clamp-2 text-sm leading-snug">{item.body}</span>}
        {(workspaceName || item.resolvedAt) && (
          <span className="mt-1 flex flex-wrap gap-1.5">
            {workspaceName && (
              <span className="bg-n200 text-n700 rounded-full px-2 py-0.5 text-xs">
                {t("workspace", { name: workspaceName })}
              </span>
            )}
            {item.resolvedAt && (
              <span className="bg-n200 text-n700 rounded-full px-2 py-0.5 text-xs">{t("resolved")}</span>
            )}
          </span>
        )}
      </span>
    </button>
  )
}
