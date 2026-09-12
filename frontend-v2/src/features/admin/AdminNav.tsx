import { useTranslation } from "react-i18next"
import { Link } from "react-router"
import { cn } from "@/shared/lib/cn"
import { ADMIN_SECTIONS, ADMIN_SECTION_PATHS, type AdminSection } from "./sections"

/**
 * The console's left column — same vocabulary and the same breakpoint as
 * SettingsNav, so both takeovers sit against the start edge and reflow into a
 * scrolling row of pills at the same width.
 */
export function AdminNav({ active }: { active: AdminSection }) {
  const { t } = useTranslation("admin")
  return (
    <nav
      aria-label={t("console.title")}
      className="border-hair flex flex-none gap-0.5 overflow-x-auto border-b px-3 py-2 sm:w-52 sm:flex-col sm:overflow-x-visible sm:overflow-y-auto sm:border-e sm:border-b-0 sm:py-3.5 sm:ps-6.5 sm:pe-3"
    >
      {ADMIN_SECTIONS.map((section) => (
        <Link
          key={section}
          to={ADMIN_SECTION_PATHS[section]}
          aria-current={active === section ? "page" : undefined}
          className={cn(
            "flex min-h-9 flex-none items-center rounded-full px-3.5 text-start text-sm whitespace-nowrap",
            active === section ? "bg-n300 text-ink font-medium" : "text-n800 hover:bg-hairsoft",
          )}
        >
          {t(`nav.${section}`)}
        </Link>
      ))}
    </nav>
  )
}
