// Unread notifications that concern the authorization centre (expired
// logins, desktop rebuilt, posts published). Dismiss = mark read.
import { useTranslation } from "react-i18next"
import { Bell, X } from "lucide-react"
import type { AppNotification } from "../types"

interface Props {
  items: AppNotification[]
  onRead: (id: string) => void
}

export function NotificationStrip({ items, onRead }: Props) {
  const { t } = useTranslation("auth-center")
  if (items.length === 0) return null
  return (
    <div className="border-hair bg-card flex flex-col gap-1.5 rounded-2xl border p-3">
      <div className="text-n700 flex items-center gap-1.5 text-xs font-medium">
        <Bell size={14} />
        {t("notifications.title", { count: items.length })}
      </div>
      {items.map((n) => (
        <div key={n.id} className="flex items-start gap-2 text-sm">
          <div className="min-w-0 flex-1">
            <span className="text-ink">{n.title}</span>
            {n.body ? <span className="text-n600 ms-2 text-xs">{n.body}</span> : null}
          </div>
          <button
            type="button"
            onClick={() => onRead(n.id)}
            aria-label={t("notifications.markRead")}
            title={t("notifications.markRead")}
            className="text-n600 hover:bg-hairsoft flex size-6 flex-none items-center justify-center rounded-full"
          >
            <X size={12} />
          </button>
        </div>
      ))}
    </div>
  )
}
