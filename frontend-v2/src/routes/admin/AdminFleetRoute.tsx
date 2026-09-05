import { Navigate } from "react-router"
import { useTranslation } from "react-i18next"
import { FleetPage } from "@/features/admin"
import { useAuthStore } from "@/shared/api/auth-store"
import { paths } from "@/shared/router/paths"


export default function AdminFleetRoute() {
  const { t } = useTranslation("admin")
  const role = useAuthStore((state) => state.user?.role)
  if (role !== "admin") return <Navigate to={paths.app} replace />
  return (
    <div className="scr min-h-0 flex-1 overflow-auto px-6.5 pt-1.5 pb-7">
      <div className="mx-auto flex w-full max-w-[1180px] flex-col gap-4.5">
        <div>
          <h1 className="text-2xl font-medium tracking-tight">{t("title")}</h1>
          <p className="mt-1 text-sm text-n600">{t("subtitle")}</p>
        </div>
        <FleetPage />
      </div>
    </div>
  )
}
