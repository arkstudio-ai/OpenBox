import { useTranslation } from "react-i18next"
import { Link } from "react-router"
import { cn } from "@/shared/lib/cn"
import { ADMIN_SECTIONS, ADMIN_SECTION_PATHS, type AdminSection } from "./sections"

/**
 * Left rail of the console — same pill vocabulary as SettingsNav, and the same
 * container-query breakpoint, so it wraps into a row of pills on narrow panes.
 */
export function AdminNav({ active }: { active: AdminSection }) {
  const { t } = useTranslation("admin")
  return (
    <nav
      aria-label={t("console.title")}
      className="flex w-full flex-none flex-wrap gap-0.5 @min-[640px]/admin:w-44 @min-[640px]/admin:flex-col"
    >
      {ADMIN_SECTIONS.map((section) => (
        <Link
          key={section}
          to={ADMIN_SECTION_PATHS[section]}
          aria-current={active === section ? "page" : undefined}
          className={cn(
            "flex min-h-9 items-center rounded-full px-3.5 text-start text-sm",
            active === section ? "bg-n300 text-ink font-medium" : "text-n800 hover:bg-hairsoft",
          )}
        >
          {t(`nav.${section}`)}
        </Link>
      ))}
    </nav>
  )
}
