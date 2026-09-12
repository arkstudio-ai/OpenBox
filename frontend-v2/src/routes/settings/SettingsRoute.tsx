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
 * Panel geometry from DEEIX-Chat's `app-settings-panel`: the rail and the
 * content ride together in one centred 1230px column, the rail capped at 16rem
 * and the content at 1080px, and the pair stacks below `xl`. The content is
 * dropped well down the page on a wide window so the section heading clears
 * the rail's title rather than racing it.
 */
export default function SettingsRoute() {
  const { t } = useTranslation("settings")
  const { tab } = useParams()
  if (tab === "usage") return <Navigate to={paths.billing("usage")} replace />
  const active: SettingsTab = SETTINGS_TABS.includes(tab as SettingsTab) ? (tab as SettingsTab) : "account"

  return (
    <div className="flex min-h-0 w-full flex-1 overflow-hidden">
      <div className="mx-auto flex min-h-0 w-full max-w-[1230px] flex-col gap-4 overflow-hidden px-3 py-4 md:px-6 xl:flex-row xl:gap-8 xl:px-0 xl:py-6">
        <SettingsNav active={active} />
        <main className="scr min-h-0 min-w-0 flex-1 overflow-y-auto overscroll-x-none">
          <div className="mx-auto w-full max-w-[1080px] min-w-0 xl:pt-20">
            <div className="space-y-6 pb-8 md:space-y-7 xl:space-y-8 xl:pb-10">
              <section className="space-y-4 px-0.5 md:space-y-5 xl:space-y-6">
                <div className="flex min-h-9 flex-col justify-center md:min-h-10">
                  <h2 className="text-md font-semibold">{t(`nav.${active}`)}</h2>
                  <p className="text-n600 text-sm">{t(`hint.${active}`)}</p>
                </div>
                <ActivePage tab={active} />
              </section>
            </div>
          </div>
        </main>
      </div>
    </div>
  )
}
