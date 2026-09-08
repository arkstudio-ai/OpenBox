// Header card of the workspace detail page: who this is, what they are on and
// what they have left.
//
// v2 (§3-Q5) puts "adjust balance" / "comp a term" buttons in the row beside
// the plan pill. Until that ships with confirmation, an idempotency key and an
// audit trail, this card stays a read-out — please do not add one here.
import type { ReactNode } from "react"
import { useTranslation } from "react-i18next"
import { formatCredits, formatNumber } from "@/shared/lib/format"
import { StatusPill } from "@/shared/ui/StatusPill"
import { DASH, formatWhen } from "@/features/admin-billing/lib/display"
import type { WorkspaceBillingDetail } from "@/features/admin-billing/types"
import { UserCell } from "./UserCell"

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="text-2xs text-n500">{label}</dt>
      <dd className="mt-0.5 truncate text-xs text-ink">{children}</dd>
    </div>
  )
}

export function WorkspaceSummary({ detail }: { detail: WorkspaceBillingDetail }) {
  const { t } = useTranslation("admin-billing")
  const workspace = detail.workspace

  return (
    <section className="rounded-xl border border-hair bg-card p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="truncate text-base font-medium text-ink">{workspace.name}</h2>
            {workspace.is_deleted && (
              <StatusPill tone="danger" title={formatWhen(workspace.deleted_at)}>
                {t("workspace.deleted")}
              </StatusPill>
            )}
          </div>
          <p className="mt-1 truncate font-mono text-2xs text-n500">{workspace.id}</p>
        </div>
        <StatusPill tone={detail.subscription ? "ok" : "muted"}>
          {t(`plans.${detail.plan_id}`, { defaultValue: detail.plan_id })}
        </StatusPill>
      </div>

      <dl className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Field label={t("workspace.fields.kind")}>{workspace.kind}</Field>
        <Field label={t("workspace.fields.owner")}>
          <UserCell user={detail.owner} />
        </Field>
        <Field label={t("workspace.fields.members")}>{formatNumber(detail.member_count)}</Field>
        <Field label={t("workspace.fields.balance")}>{formatCredits(detail.balance)}</Field>
        <Field label={t("workspace.fields.expires")}>{formatWhen(detail.subscription?.ends_at)}</Field>
        <Field label={t("workspace.fields.queued")}>{formatNumber(detail.queued.length)}</Field>
        <Field label={t("workspace.fields.created")}>{formatWhen(workspace.created_at)}</Field>
        <Field label={t("workspace.fields.deletedAt")}>
          {workspace.deleted_at ? formatWhen(workspace.deleted_at) : DASH}
        </Field>
      </dl>

      <p className="mt-4 text-2xs text-n500">{t("workspace.readOnly")}</p>
    </section>
  )
}
