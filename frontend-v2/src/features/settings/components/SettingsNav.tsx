import { useTranslation } from "react-i18next"
import { useNavigate } from "react-router"
import { paths } from "@/shared/router/paths"
import { cn } from "@/shared/lib/cn"
import { SETTINGS_TABS, type SettingsTab } from "@/features/settings/tabs"

/**
 * The settings rail, following DEEIX-Chat's `settings-sidebar`: the page title
 * sits above the list rather than in the chrome, the items are full-width
 * rounded rectangles rather than pills, and the column sticks while the
 * content scrolls. Below the panel's breakpoint the list becomes a horizontal
 * scroller under the title — the shape that survives a phone.
 */
export function SettingsNav({ active }: { active: SettingsTab }) {
  const { t } = useTranslation("settings")
  const navigate = useNavigate()
  return (
    <aside className="w-full flex-none xl:max-w-64">
      <div className="space-y-3 xl:sticky xl:top-6 xl:space-y-5">
        <div className="flex h-9 items-center px-1 xl:h-10">
          <h1 className="text-xl font-semibold tracking-normal xl:text-2xl">{t("title")}</h1>
        </div>
        <nav
          aria-label={t("title")}
          className="scr flex gap-1.5 overflow-x-auto overscroll-x-contain pb-1 xl:grid xl:gap-1 xl:overflow-visible xl:pb-0"
        >
          {SETTINGS_TABS.map((tab) => (
            <button
              key={tab}
              type="button"
              onClick={() => navigate(paths.settings(tab))}
              aria-current={active === tab ? "page" : undefined}
              className={cn(
                "focus-visible:ring-a700/35 flex h-8 flex-none items-center rounded-md px-3 text-sm font-medium whitespace-nowrap outline-hidden focus-visible:ring-2 xl:h-9 xl:w-full xl:px-3.5",
                active === tab ? "bg-n200 text-ink" : "text-n800 hover:bg-hairsoft",
              )}
            >
              {t(`nav.${tab}`)}
            </button>
          ))}
        </nav>
      </div>
    </aside>
  )
}
