// Every term this workspace ever bought, newest first, with the live one and
// the queued renewals marked — the two facts a renewal question turns on.
import { useTranslation } from "react-i18next"
import { DataTable } from "@/shared/ui/DataTable"
import { StatusPill } from "@/shared/ui/StatusPill"
import { DASH, formatWhen } from "@/features/admin-billing/lib/display"
import type { SubscriptionTerm, WorkspaceBillingDetail } from "@/features/admin-billing/types"
import { SectionCard } from "./SectionCard"

export function WorkspaceTerms({ detail }: { detail: WorkspaceBillingDetail }) {
  const { t } = useTranslation("admin-billing")
  const column = (key: string) => t(`workspace.terms.columns.${key}`)
  const queued = new Set(detail.queued.map((term) => term.order_id))
  const phase = (term: SubscriptionTerm) => {
    if (term.order_id === detail.subscription?.order_id) {
      return <StatusPill tone="ok">{t("workspace.terms.current")}</StatusPill>
    }
    if (queued.has(term.order_id)) {
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
            render: (term) => <span className="block truncate">{term.order_id}</span>,
          },
          {
            key: "plan_id",
            header: column("plan"),
            render: (term) => t(`plans.${term.plan_id}`, { defaultValue: term.plan_id }),
          },
          {
            key: "cycle",
            header: column("cycle"),
            render: (term) =>
              term.cycle ? t(`cycles.${term.cycle}`, { defaultValue: term.cycle }) : DASH,
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
        ]}
        rows={detail.history}
        rowKey={(term) => term.order_id}
        emptyText={t("workspace.terms.empty")}
        errorText={t("list.error")}
        loadingLabel={t("list.loading")}
        minWidth="min-w-[48rem]"
      />
    </SectionCard>
  )
}
