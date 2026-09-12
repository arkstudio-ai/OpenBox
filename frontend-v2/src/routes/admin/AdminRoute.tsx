import { Outlet, useLocation } from "react-router"
import { useTranslation } from "react-i18next"
import { AdminNav, activeAdminSection, adminLayout } from "@/features/admin"
import { cn } from "@/shared/lib/cn"

/**
 * Console shell: one page title for the whole console, the column rail on the
 * start side, and the active column's own heading above its content. The rail
 * collapses into a row of wrapping pills below the SettingsRoute breakpoint —
 * same container query, so both consoles reflow at the same width. The
 * trajectory column drops the reading-width cap (timeline, table and inspector
 * side by side) but keeps the rail, so the console stays one click away.
 */
export default function AdminRoute() {
  const { t } = useTranslation("admin")
  const { pathname } = useLocation()
  const active = activeAdminSection(pathname)
  const layout = adminLayout(pathname)

  return (
    <div className="scr @container/admin min-h-0 flex-1 overflow-auto px-6.5 pt-1.5 pb-7">
      <div className={cn("mx-auto flex w-full flex-col gap-4.5", !layout.wide && "max-w-[1180px]")}>
        <div className="flex flex-col gap-1">
          <h1 className="text-2xl font-medium tracking-tight">{t("console.title")}</h1>
          <p className="text-n600 text-sm">{t("console.subtitle")}</p>
        </div>
        <div className="flex flex-col items-stretch gap-5 @min-[640px]/admin:flex-row @min-[640px]/admin:items-start @min-[640px]/admin:gap-7">
          <AdminNav active={active} />
          <div className="flex min-w-0 flex-1 flex-col gap-4.5">
            {layout.heading && (
              <div className="flex flex-col gap-1">
                <h2 className="text-lg font-medium tracking-tight">{t(`section.${active}.title`)}</h2>
                <p className="text-n600 text-sm">{t(`section.${active}.subtitle`)}</p>
              </div>
            )}
            <Outlet />
          </div>
        </div>
      </div>
    </div>
  )
}
