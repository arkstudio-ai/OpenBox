import { Link, useParams } from "react-router"
import { useTranslation } from "react-i18next"
import { ADMIN_BILLING_TABS, SubscriptionsPage, OrdersPage } from "@/features/admin-billing"
import { paths } from "@/shared/router/paths"
import { cn } from "@/shared/lib/cn"

type AdminBillingTab = (typeof ADMIN_BILLING_TABS)[number]

export default function AdminBillingRoute() {
  const { t } = useTranslation("admin-billing")
  const { tab } = useParams()
  const active: AdminBillingTab = ADMIN_BILLING_TABS.find((value) => value === tab) ?? ADMIN_BILLING_TABS[0]

  return (
    <div className="flex flex-col gap-4.5">
      <nav className="flex flex-wrap gap-1" aria-label={t("title")}>
        {ADMIN_BILLING_TABS.map((value) => (
          <Link
            key={value}
            to={paths.adminBilling(value)}
            aria-current={active === value ? "page" : undefined}
            className={cn(
              "rounded-full px-4 py-2 text-sm",
              active === value ? "bg-n300 text-ink font-medium" : "text-n700 hover:bg-n200",
            )}
          >
            {t(`tabs.${value}`)}
          </Link>
        ))}
      </nav>
      {active === "orders" ? <OrdersPage /> : <SubscriptionsPage />}
    </div>
  )
}
