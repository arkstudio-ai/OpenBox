// Everything the operator needs about one workspace's money, on one page.
//
// Deliberately read-only. §3-Q5 defers adjustments, comped payments, refunds
// and expiry edits to v2, where each needs a confirmation dialog, an
// idempotency key and an audit record — so this page has no action buttons at
// all, and adding a "quick" one here would ship a money write with none of it.
import { useTranslation } from "react-i18next"
import { Link } from "react-router"
import { ApiError } from "@/shared/api/http"
import { paths } from "@/shared/router/paths"
import { Spinner } from "@/shared/ui/Spinner"
import { useWorkspaceBilling } from "@/features/admin-billing/api/admin-billing"
import {
  DETAIL_LEDGER_LIMIT,
  DETAIL_ORDER_LIMIT,
} from "@/features/admin-billing/lib/filters"
import { WorkspaceLedger } from "./WorkspaceLedger"
import { WorkspaceOrders } from "./WorkspaceOrders"
import { WorkspaceSummary } from "./WorkspaceSummary"
import { WorkspaceTerms } from "./WorkspaceTerms"
import { WorkspaceUsage } from "./WorkspaceUsage"

export function WorkspaceDetailPage({ workspaceId }: { workspaceId: string }) {
  const { t } = useTranslation("admin-billing")
  const detail = useWorkspaceBilling(workspaceId)
  // A 404 is the "empty" state here: the id in the address bar names nothing.
  const missing = detail.error instanceof ApiError && detail.error.status === 404

  return (
    <div className="flex flex-col gap-4">
      <Link
        to={paths.adminBilling("subscriptions")}
        className="self-start text-xs text-n600 hover:text-ink"
      >
        {t("workspace.back")}
      </Link>

      {detail.isPending ? (
        <div className="flex items-center justify-center py-16">
          <Spinner className="size-5" />
          <span className="sr-only">{t("workspace.loading")}</span>
        </div>
      ) : missing ? (
        <p className="py-10 text-center text-sm text-n600">{t("workspace.notFound")}</p>
      ) : detail.error || !detail.data ? (
        <p role="alert" className="py-10 text-center text-sm text-danger">
          {t("workspace.error")}
        </p>
      ) : (
        <>
          <WorkspaceSummary detail={detail.data} />
          <WorkspaceTerms detail={detail.data} />
          <WorkspaceOrders orders={detail.data.orders} limit={DETAIL_ORDER_LIMIT} />
          <WorkspaceLedger entries={detail.data.ledger} limit={DETAIL_LEDGER_LIMIT} />
          <WorkspaceUsage usage={detail.data.usage} />
        </>
      )}
    </div>
  )
}
