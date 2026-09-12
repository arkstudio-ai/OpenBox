import { useTranslation } from "react-i18next"
import { useNavigate } from "react-router"
import { paths } from "@/shared/router/paths"
import { cn } from "@/shared/lib/cn"
import { SETTINGS_TABS, type SettingsTab } from "@/features/settings/tabs"

/**
 * The settings takeover's left column. It is the only rail on screen — the
 * workspace sidebar steps aside for this page — so it sits against the start
 * edge rather than floating beside a centred block, and its padding lines its
 * pills up under the topbar's back link. Below the breakpoint the column
 * becomes a scrolling row of pills above the content, the one shape that
 * survives a phone.
 */
export function SettingsNav({ active }: { active: SettingsTab }) {
  const { t } = useTranslation("settings")
  const navigate = useNavigate()
  return (
    <nav
      aria-label={t("title")}
      className="border-hair flex flex-none gap-0.5 overflow-x-auto border-b px-3 py-2 sm:w-52 sm:flex-col sm:overflow-x-visible sm:overflow-y-auto sm:border-e sm:border-b-0 sm:py-3.5 sm:ps-6.5 sm:pe-3"
    >
      {SETTINGS_TABS.map((tab) => (
        <button
          key={tab}
          type="button"
          onClick={() => navigate(paths.settings(tab))}
          aria-current={active === tab ? "page" : undefined}
          className={cn(
            "flex min-h-9 flex-none items-center rounded-full px-3.5 text-start text-sm whitespace-nowrap",
            active === tab ? "bg-n300 text-ink font-medium" : "text-n800 hover:bg-hairsoft",
          )}
        >
          {t(`nav.${tab}`)}
        </button>
      ))}
    </nav>
  )
}
