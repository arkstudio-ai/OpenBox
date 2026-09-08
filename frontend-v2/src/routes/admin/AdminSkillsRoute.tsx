import { Link, useParams } from "react-router"
import { useTranslation } from "react-i18next"
import {
  ADMIN_SKILL_TABS,
  isAdminSkillTab,
  StorePage,
  ReviewPage,
  InstallsPage,
  type AdminSkillTab,
} from "@/features/admin-skills"
import { paths } from "@/shared/router/paths"
import { cn } from "@/shared/lib/cn"

function ActivePage({ tab }: { tab: AdminSkillTab }) {
  switch (tab) {
    case "review":
      return <ReviewPage />
    case "installs":
      return <InstallsPage />
    default:
      return <StorePage />
  }
}

export default function AdminSkillsRoute() {
  const { t } = useTranslation("admin-skills")
  const { tab } = useParams()
  const active = isAdminSkillTab(tab) ? tab : ADMIN_SKILL_TABS[0]

  return (
    <div className="flex flex-col gap-4.5">
      <nav className="flex flex-wrap gap-1" aria-label={t("title")}>
        {ADMIN_SKILL_TABS.map((value) => (
          <Link
            key={value}
            to={paths.adminSkills(value)}
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
