// Every term this workspace ever bought, newest first, with the live one and
// the queued renewals marked — the two facts a renewal question turns on.
import { useTranslation } from "react-i18next"
import { DataTable } from "@/shared/ui/DataTable"
import { StatusPill } from "@/shared/ui/StatusPill"
import { DASH, formatWhen } from "@/features/admin-billing/lib/display"
import type { SubscriptionTerm, WorkspaceBillingDetail } from "@/features/admin-billing/types"
import { SectionCard } from "./SectionCard"

export function WorkspaceTerms({
  detail,
  onAction,
}: {
  detail: WorkspaceBillingDetail
  onAction?: (kind: "change" | "cancel", term: SubscriptionTerm) => void
}) {
  const { t } = useTranslation("admin-billing")
  const column = (key: string) => t(`workspace.terms.columns.${key}`)
  const identity = (term: SubscriptionTerm) => term.id ?? term.order_id ?? ""
  const queued = new Set(detail.queued.map(identity))
  const phase = (term: SubscriptionTerm) => {
    if (term.cancelled_at) return <StatusPill tone="muted">{t("workspace.terms.cancelled")}</StatusPill>
    if (detail.subscription && identity(term) === identity(detail.subscription)) {
      return <StatusPill tone="ok">{t("workspace.terms.current")}</StatusPill>
    }
    if (queued.has(identity(term))) {
      return <StatusPill tone="accent">{t("workspace.terms.queued")}</StatusPill>
    }
    return <span className="text-n500">{DASH}</span>
  }

  return (
    <SectionCard title={t("workspace.terms.title")}>
      <DataTable<SubscriptionTerm>
        columns={[
          {
            key: "order_id",
            header: column("orderId"),
            className: "max-w-[13rem] font-mono text-2xs",
            render: (term) => (
              <span className="block truncate">{term.order_id ?? t("workspace.terms.adminSource")}</span>
            ),
          },
          {
            key: "plan_id",
            header: column("plan"),
            render: (term) => t(`plans.${term.plan_id}`, { defaultValue: term.plan_id }),
          },
          {
            key: "cycle",
            header: column("cycle"),
            render: (term) => (term.cycle ? t(`cycles.${term.cycle}`, { defaultValue: term.cycle }) : DASH),
          },
          {
            key: "starts_at",
            header: column("startsAt"),
            className: "whitespace-nowrap",
            render: (term) => formatWhen(term.starts_at),
          },
          {
            key: "ends_at",
            header: column("endsAt"),
            className: "whitespace-nowrap",
            render: (term) => formatWhen(term.ends_at),
          },
          { key: "phase", header: column("phase"), render: phase },
          ...(onAction
            ? [
                {
                  key: "actions",
                  header: t("actions.manage"),
                  render: (term: SubscriptionTerm) =>
                    term.id &&
                    term.revision &&
                    !term.cancelled_at &&
                    new Date(term.ends_at ?? "").getTime() > Date.now() ? (
                      <div className="flex gap-3 whitespace-nowrap">
                        <button
                          type="button"
                          onClick={() => onAction("change", term)}
                          className="text-ink text-xs underline"
                        >
                          {t("actions.change")}
                        </button>
                        <button
                          type="button"
                          onClick={() => onAction("cancel", term)}
                          className="text-danger text-xs underline"
                        >
                          {t("actions.cancel")}
                        </button>
                      </div>
                    ) : (
                      DASH
                    ),
                },
              ]
            : []),
        ]}
        rows={detail.history}
        rowKey={identity}
        emptyText={t("workspace.terms.empty")}
        errorText={t("list.error")}
        loadingLabel={t("list.loading")}
        minWidth="min-w-[48rem]"
      />
    </SectionCard>
  )
}
