import { useTranslation } from "react-i18next"
import { Navigate, useParams } from "react-router"
import { paths } from "@/shared/router/paths"
import {
  SettingsNav,
  SETTINGS_TABS,
  type SettingsTab,
  AccountPage,
  ModelsPage,
  BrowserPage,
  AppearancePage,
  TeamPage,
} from "@/features/settings"

function ActivePage({ tab }: { tab: SettingsTab }) {
  switch (tab) {
    case "team":
      return <TeamPage />
    case "models":
      return <ModelsPage />
    case "browser":
      return <BrowserPage />
    case "appearance":
      return <AppearancePage />
    default:
      return <AccountPage />
  }
}

/**
 * Two panes: the section rail against the start edge, the section itself
 * beside it. The rail is anchored rather than centred with the content —
 * centring the pair left it floating mid-screen with the "left menu" nowhere
 * near the left. The section keeps the chat transcript's reading width and
 * starts where the rail ends, so the eye travels rail → heading → content
 * instead of across a gap.
 */
export default function SettingsRoute() {
  const { t } = useTranslation("settings")
  const { tab } = useParams()
  if (tab === "usage") return <Navigate to={paths.billing("usage")} replace />
  const active: SettingsTab = SETTINGS_TABS.includes(tab as SettingsTab) ? (tab as SettingsTab) : "account"

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-hidden sm:flex-row">
      <SettingsNav active={active} />
      <div className="scr min-h-0 flex-1 overflow-auto px-3 pt-2.5 pb-7 sm:px-6.5 sm:pt-1.5">
        <div className="flex w-full max-w-190 flex-col gap-4.5">
          <div className="flex flex-col gap-1">
            <h1 className="text-2xl font-medium tracking-tight">{t(`nav.${active}`)}</h1>
            <span className="text-n600 text-sm">{t(`hint.${active}`)}</span>
          </div>
          <ActivePage tab={active} />
        </div>
      </div>
    </div>
  )
}
