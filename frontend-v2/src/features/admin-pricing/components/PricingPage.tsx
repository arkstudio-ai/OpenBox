// Every billable item on one page: cost beside sale price, margin, and the
// last 30 days of use. Filtering is client-side — the table is one read and
// small enough to hold.
import { useMemo, useState } from "react"
import { useTranslation } from "react-i18next"
import { useUrlState } from "@/shared/hooks/useUrlState"
import { DataTable } from "@/shared/ui/DataTable"
import { formatCredits, formatDateTime } from "@/shared/lib/format"
import { exportUrl, usePricingTable } from "../api"
import { FILTER_FLAGS, formatMargin } from "../lib"
import { PRICING_KINDS, type PricingFlag, type PricingItem } from "../types"
import { FilterSelect, SearchField } from "./Filters"
import { PricingEditSheet } from "./PricingEditSheet"
import { usePricingColumns } from "./pricingColumns"

const ALL = "all"
const DEFAULTS = { kind: ALL, flag: ALL, q: "" }

function SummaryTile({ label, value, tone }: { label: string; value: string; tone?: "danger" | "ok" }) {
  return (
    <div className="rounded-xl border border-hair bg-card px-4 py-3">
      <p className="text-n600 text-xs">{label}</p>
      <p className={`mt-1 font-mono text-lg tabular-nums ${tone === "danger" ? "text-dangerink" : tone === "ok" ? "text-s800" : "text-ink"}`}>
        {value}
      </p>
    </div>
  )
}

export function PricingPage() {
  const { t } = useTranslation("admin-pricing")
  const [values, setValues] = useUrlState(DEFAULTS)
  const table = usePricingTable()
  const [editing, setEditing] = useState<PricingItem | null>(null)
  const columns = usePricingColumns(setEditing)

  const rows = useMemo(() => {
    const items = table.data?.items ?? []
    const q = values.q.trim().toLowerCase()
    return items.filter(
      (item) =>
        (values.kind === ALL || item.kind === values.kind) &&
        (values.flag === ALL || item.flags.includes(values.flag as PricingFlag)) &&
        (!q || item.key.toLowerCase().includes(q) || item.label.toLowerCase().includes(q)),
    )
  }, [table.data, values])

  const summary = table.data?.summary
  const grossPct =
    summary && Number(summary.cost_credits) > 0
      ? (((Number(summary.credits) - Number(summary.cost_credits)) / Number(summary.cost_credits)) * 100).toFixed(1)
      : null
  // The sheet edits the live row: after a save the table refetches and the
  // row object changes, so look the open item up again by key.
  const open = editing ? (table.data?.items.find((item) => item.key === editing.key) ?? editing) : null

  return (
    <div className="flex flex-col gap-4">
      {summary && (
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
          <SummaryTile label={t("summary.credits", { days: table.data?.window_days })} value={formatCredits(summary.credits)} />
          <SummaryTile label={t("summary.cost")} value={formatCredits(summary.cost_credits)} />
          <SummaryTile
            label={t("summary.gross")}
            value={`${formatCredits(summary.gross_margin)}${grossPct ? ` (${formatMargin(grossPct)})` : ""}`}
            tone={Number(summary.gross_margin) < 0 ? "danger" : "ok"}
          />
          <SummaryTile
            label={t("summary.attention")}
            value={String((summary.flags.below_cost ?? 0) + (summary.flags.unpriced ?? 0) + (summary.flags.expiring ?? 0))}
            tone={(summary.flags.below_cost ?? 0) + (summary.flags.unpriced ?? 0) > 0 ? "danger" : undefined}
          />
        </div>
      )}

      <form
        className="flex flex-wrap items-end gap-3 rounded-xl border border-hair bg-card p-4"
        aria-label={t("filters.label")}
        onSubmit={(event) => event.preventDefault()}
      >
        <FilterSelect
          label={t("filters.kind")}
          value={values.kind}
          options={[{ value: ALL, label: t("filters.all") }, ...PRICING_KINDS.map((kind) => ({ value: kind, label: t(`kinds.${kind}`) }))]}
          onChange={(kind) => setValues({ kind })}
        />
        <FilterSelect
          label={t("filters.flag")}
          value={values.flag}
          options={[{ value: ALL, label: t("filters.all") }, ...FILTER_FLAGS.map((flag) => ({ value: flag, label: t(`flags.${flag}`) }))]}
          onChange={(flag) => setValues({ flag })}
        />
        <SearchField label={t("filters.keyword")} placeholder={t("filters.placeholder")} value={values.q} onChange={(q) => setValues({ q })} />
        <div className="text-n500 ml-auto flex flex-col items-end gap-1 text-2xs">
          {table.data && (
            <span>
              {t("summary.catalogue", { version: table.data.catalogue_version })}
              {" · "}
              {t("summary.effective", { seconds: table.data.effective_within_seconds })}
              {table.data.rules_loaded_at && ` · ${t("summary.loaded", { at: formatDateTime(table.data.rules_loaded_at) })}`}
            </span>
          )}
          <a href={exportUrl()} download className="text-ink underline">
            {t("summary.export")}
          </a>
        </div>
      </form>

      <section className="rounded-xl border border-hair bg-card p-4">
        <DataTable
          columns={columns}
          rows={rows}
          rowKey={(item) => item.key}
          onRowClick={setEditing}
          isLoading={table.isLoading}
          error={table.error}
          emptyText={t("table.empty")}
          errorText={t("table.error")}
          loadingLabel={t("table.loading")}
          minWidth="min-w-[72rem]"
        />
      </section>

      {open && <PricingEditSheet item={open} onClose={() => setEditing(null)} />}
    </div>
  )
}
