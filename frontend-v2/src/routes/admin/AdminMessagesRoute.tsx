import { Link, useParams } from "react-router"
import { useTranslation } from "react-i18next"
import {
  ADMIN_MESSAGE_TABS,
  isAdminMessageTab,
  AnnouncementsPage,
  TopicsPage,
  type AdminMessageTab,
} from "@/features/admin-messages"
import { paths } from "@/shared/router/paths"
import { cn } from "@/shared/lib/cn"

function ActivePage({ tab }: { tab: AdminMessageTab }) {
  return tab === "topics" ? <TopicsPage /> : <AnnouncementsPage />
}

export default function AdminMessagesRoute() {
  const { t } = useTranslation("admin-messages")
  const { tab } = useParams()
  const active = isAdminMessageTab(tab) ? tab : ADMIN_MESSAGE_TABS[0]

  return (
    <div className="flex flex-col gap-4.5">
      <nav className="flex flex-wrap gap-1" aria-label={t("title")}>
        {ADMIN_MESSAGE_TABS.map((value) => (
          <Link
            key={value}
            to={paths.adminMessages(value)}
            aria-current={active === value ? "page" : undefined}
            className={cn(
              "rounded-full px-4 py-2 text-sm",
              active === value ? "bg-n300 text-ink font-medium" : "text-n700 hover:bg-n200",
            )}
          >
            {t(`tab.${value}`)}
          </Link>
        ))}
      </nav>
      <ActivePage tab={active} />
    </div>
  )
}
