import { Outlet, useLocation } from "react-router"
import { useTranslation } from "react-i18next"
import { AdminNav, activeAdminSection, adminLayout } from "@/features/admin"
import { cn } from "@/shared/lib/cn"

/**
 * Console shell, built the same way as SettingsRoute: the column rail against
 * the start edge and the active column beside it, rather than the pair centred
 * together — centred, the rail floated mid-screen and stopped reading as a
 * left menu at all. The console names itself once, in the topbar beside the
 * way out; repeating it here put the same words on screen twice with the
 * column's own heading under them. The rail reflows into a scrolling row of
 * pills at the SettingsRoute breakpoint — the takeover owns the whole window,
 * so plain viewport widths decide it and both consoles behave alike on a
 * phone. The trajectory column drops the reading-width cap (timeline, table
 * and inspector side by side) but keeps the rail, so the console stays one
 * click away.
 */
export default function AdminRoute() {
  const { t } = useTranslation("admin")
  const { pathname } = useLocation()
  const active = activeAdminSection(pathname)
  const layout = adminLayout(pathname)

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-hidden sm:flex-row">
      <AdminNav active={active} />
      <div className="scr min-h-0 flex-1 overflow-auto px-3 pt-2.5 pb-7 sm:px-6.5 sm:pt-1.5">
        <div className={cn("flex w-full flex-col gap-4.5", !layout.wide && "max-w-[1180px]")}>
          {layout.heading && (
            <div className="flex flex-col gap-1">
              <h1 className="text-2xl font-medium tracking-tight">{t(`section.${active}.title`)}</h1>
              <p className="text-n600 text-sm">{t(`section.${active}.subtitle`)}</p>
            </div>
          )}
          <Outlet />
        </div>
      </div>
    </div>
  )
}
