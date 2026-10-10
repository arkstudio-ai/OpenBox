import { useState } from "react"
import { useTranslation } from "react-i18next"
import { Link } from "react-router"
import { ApiError } from "@/shared/api/http"
import { useAuthStore } from "@/shared/api/auth-store"
import { paths } from "@/shared/router/paths"
import { Spinner } from "@/shared/ui/Spinner"
import { useWorkspaceBilling } from "@/features/admin-billing/api/admin-billing"
import { DETAIL_LEDGER_LIMIT, DETAIL_ORDER_LIMIT } from "@/features/admin-billing/lib/filters"
import { WorkspaceLedger } from "./WorkspaceLedger"
import { WorkspaceOrders } from "./WorkspaceOrders"
import { WorkspaceSummary } from "./WorkspaceSummary"
import { WorkspaceTerms } from "./WorkspaceTerms"
import { WorkspaceUsage } from "./WorkspaceUsage"
import { WorkspaceOperations } from "./WorkspaceOperations"
import { BillingActionDialog } from "./BillingActionDialog"
import { pendingBillingWrite } from "@/features/admin-billing/hooks/useBillingActionForm"
import type { BillingActionKind, BillingWrite, SubscriptionTerm } from "@/features/admin-billing/types"

export function WorkspaceDetailPage({ workspaceId }: { workspaceId: string }) {
  const { t } = useTranslation("admin-billing")
  const detail = useWorkspaceBilling(workspaceId)
  const user = useAuthStore((s) => s.user)
  const [action, setAction] = useState<{
    actor: string
    workspace: string
    kind: BillingActionKind
    term?: SubscriptionTerm
    pending?: BillingWrite | null
  } | null>(null)
  const [saved, setSaved] = useState(false)
  const allowed =
    user?.role === "admin" && detail.data?.can_manage === true && !detail.data.workspace.is_deleted
  const openAction = (kind: BillingActionKind, term?: SubscriptionTerm) => {
    const pending = pendingBillingWrite(user?.id ?? "anonymous", workspaceId)
    setSaved(false)
    const scope = { actor: user?.id ?? "anonymous", workspace: workspaceId }
    setAction(
      pending
        ? {
            ...scope,
            kind: pending.kind,
            term: detail.data?.history.find((row) => row.id === pending.subscriptionId),
            pending,
          }
        : { ...scope, kind, term },
    )
  }
  // A 404 is the "empty" state here: the id in the address bar names nothing.
  const missing = detail.error instanceof ApiError && detail.error.status === 404

  return (
    <div className="flex flex-col gap-4">
      <Link to={paths.adminBilling("subscriptions")} className="text-n600 hover:text-ink self-start text-xs">
        {t("workspace.back")}
      </Link>

      {detail.isPending ? (
        <div className="flex items-center justify-center py-16">
          <Spinner className="size-5" />
          <span className="sr-only">{t("workspace.loading")}</span>
        </div>
      ) : missing ? (
        <p className="text-n600 py-10 text-center text-sm">{t("workspace.notFound")}</p>
      ) : detail.error || !detail.data ? (
        <p role="alert" className="text-danger py-10 text-center text-sm">
          {t("workspace.error")}
        </p>
      ) : (
        <>
          <WorkspaceSummary detail={detail.data} />
          {saved && (
            <p role="status" className="text-n700 text-sm">
              {t("actions.success")}
            </p>
          )}
          {allowed && (
            <div className="flex flex-wrap gap-2">
              <button
                type="button"
                onClick={() => openAction("credits")}
                className="bg-ink text-bg rounded-full px-4 py-2 text-sm"
              >
                {t("actions.credits")}
              </button>
              <button
                type="button"
                onClick={() => openAction("grant")}
                className="border-hair text-ink rounded-full border px-4 py-2 text-sm"
              >
                {t("actions.grant")}
              </button>
            </div>
          )}
          <WorkspaceTerms detail={detail.data} onAction={allowed ? openAction : undefined} />
          <WorkspaceOrders orders={detail.data.orders} limit={DETAIL_ORDER_LIMIT} />
          <WorkspaceLedger entries={detail.data.ledger} limit={DETAIL_LEDGER_LIMIT} />
          <WorkspaceOperations operations={detail.data.operations ?? []} />
          <WorkspaceUsage usage={detail.data.usage} />
          {allowed && action && action.actor === user?.id && action.workspace === workspaceId && (
            <BillingActionDialog
              key={`${user?.id}:${workspaceId}`}
              detail={detail.data}
              {...action}
              onClose={() => setAction(null)}
              onDone={() => {
                setAction(null)
                setSaved(true)
              }}
            />
          )}
        </>
      )}
    </div>
  )
}
