import { Outlet, useLocation } from "react-router"
import { useTranslation } from "react-i18next"
import { AdminNav, activeAdminSection, adminLayout } from "@/features/admin"
import { cn } from "@/shared/lib/cn"

/**
 * Console shell on the same panel geometry as SettingsRoute (DEEIX-Chat's
 * `app-settings-panel`): rail and content in one centred column, stacking
 * below `xl`. The console names itself once, in the rail's title; the active
 * column supplies its own heading. Every column shares that one width, the
 * trajectory viewer included — a column that ran to both edges of the window
 * read as a different page, and the viewer folds itself down on its own.
 */
export default function AdminRoute() {
  const { t } = useTranslation("admin")
  const { pathname } = useLocation()
  const active = activeAdminSection(pathname)
  const layout = adminLayout(pathname)

  return (
    <div className="flex min-h-0 w-full flex-1 overflow-hidden">
      <div className="mx-auto flex min-h-0 w-full max-w-[1230px] flex-col gap-4 overflow-hidden px-3 py-4 md:px-6 xl:flex-row xl:gap-8 xl:px-0 xl:py-6">
        <AdminNav active={active} />
        <main className="scr min-h-0 min-w-0 flex-1 overflow-y-auto overscroll-x-none">
          {/* The drop only exists to let a section heading clear the rail's
              title; a column that has none starts at the top of its pane. */}
          <div className={cn("mx-auto w-full max-w-[1080px] min-w-0", layout.heading && "xl:pt-20")}>
            <div className="space-y-6 pb-8 md:space-y-7 xl:space-y-8 xl:pb-10">
              <section className="space-y-4 px-0.5 md:space-y-5 xl:space-y-6">
                {layout.heading && (
                  <div className="flex min-h-9 flex-col justify-center md:min-h-10">
                    <h2 className="text-md font-semibold">{t(`section.${active}.title`)}</h2>
                    <p className="text-n600 text-sm">{t(`section.${active}.subtitle`)}</p>
                  </div>
                )}
                <Outlet />
              </section>
            </div>
          </div>
        </main>
      </div>
    </div>
  )
}
