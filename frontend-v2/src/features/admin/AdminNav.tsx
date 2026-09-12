import { useTranslation } from "react-i18next"
import { Link } from "react-router"
import { cn } from "@/shared/lib/cn"
import { ADMIN_SECTIONS, ADMIN_SECTION_PATHS, type AdminSection } from "./sections"

/**
 * The console rail — same shape as SettingsNav, so both takeovers read as one
 * page type: title above the list, full-width rounded rectangles, sticky on a
 * wide window and a horizontal scroller below the panel's breakpoint.
 */
export function AdminNav({ active }: { active: AdminSection }) {
  const { t } = useTranslation("admin")
  return (
    <aside className="w-full flex-none xl:max-w-64">
      <div className="space-y-3 xl:sticky xl:top-6 xl:space-y-5">
        <div className="flex h-9 items-center px-1 xl:h-10">
          <h1 className="text-xl font-semibold tracking-normal xl:text-2xl">{t("console.title")}</h1>
        </div>
        <nav
          aria-label={t("console.title")}
          className="scr flex gap-1.5 overflow-x-auto overscroll-x-contain pb-1 xl:grid xl:gap-1 xl:overflow-visible xl:pb-0"
        >
          {ADMIN_SECTIONS.map((section) => (
            <Link
              key={section}
              to={ADMIN_SECTION_PATHS[section]}
              aria-current={active === section ? "page" : undefined}
              className={cn(
                "focus-visible:ring-a700/35 flex h-8 flex-none items-center rounded-md px-3 text-sm font-medium whitespace-nowrap outline-hidden focus-visible:ring-2 xl:h-9 xl:w-full xl:px-3.5",
                active === section ? "bg-n200 text-ink" : "text-n800 hover:bg-hairsoft",
              )}
            >
              {t(`nav.${section}`)}
            </Link>
          ))}
        </nav>
      </div>
    </aside>
  )
}
