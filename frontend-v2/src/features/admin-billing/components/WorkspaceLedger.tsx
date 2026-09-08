// The tail of the credit ledger: every movement arrives as a Decimal string
// and is rendered as one, so the column adds up against the database.
import { useTranslation } from "react-i18next"
import { formatCredits, formatNumber } from "@/shared/lib/format"
import { DataTable } from "@/shared/ui/DataTable"
import { DASH, formatWhen } from "@/features/admin-billing/lib/display"
import type { LedgerEntry } from "@/features/admin-billing/types"
import { SectionCard } from "./SectionCard"

export function WorkspaceLedger({ entries, limit }: { entries: LedgerEntry[]; limit: number }) {
  const { t } = useTranslation("admin-billing")
  const column = (key: string) => t(`workspace.ledger.columns.${key}`)

  return (
    <SectionCard
      title={t("workspace.ledger.title")}
      hint={t("workspace.ledger.hint", { value: formatNumber(limit) })}
    >
      <DataTable<LedgerEntry>
        columns={[
          {
            key: "created_at",
            header: column("createdAt"),
            className: "whitespace-nowrap",
            render: (entry) => formatWhen(entry.created_at),
          },
          { key: "kind", header: column("kind") },
          {
            key: "amount",
            header: column("amount"),
            className: "tabular-nums",
            render: (entry) => formatCredits(entry.amount),
          },
          {
            key: "balance_after",
            header: column("balanceAfter"),
            className: "tabular-nums",
            render: (entry) => formatCredits(entry.balance_after),
          },
          {
            key: "reference_id",
            header: column("reference"),
            className: "max-w-[13rem] font-mono text-2xs",
            render: (entry) => (
              <span className="block truncate">{entry.reference_id || DASH}</span>
            ),
          },
        ]}
        rows={entries}
        rowKey={(entry) => entry.id}
        emptyText={t("workspace.ledger.empty")}
        errorText={t("list.error")}
        loadingLabel={t("list.loading")}
        minWidth="min-w-[40rem]"
      />
    </SectionCard>
  )
}
