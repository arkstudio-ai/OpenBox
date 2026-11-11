// The library: filters in the URL, one server-side page, previews and the
// write actions. `?collect=<session>` (from the trajectory turn page) opens
// the sheet on that session's output straight away.
import { useMemo, useState } from "react"
import { useTranslation } from "react-i18next"
import { useSearchParams } from "react-router"
import { useUrlState } from "@/shared/hooks/useUrlState"
import { formatSince } from "@/shared/lib/format"
import { DataTable, type DataTableColumn } from "@/shared/ui/DataTable"
import { StatusPill } from "@/shared/ui/StatusPill"
import { toast } from "@/shared/ui/Toast"
import { PAGE_SIZE, useExamples, useSetExampleStatus, useTaxonomy } from "./api"
import { AssetTile, ExampleSheet, type SheetMode } from "./ExampleSheet"
import type { Example } from "./types"

const DEFAULTS = { entry: "", industry: "", shopType: "", status: "", q: "", offset: "0" }
const SELECT = "border-hair bg-bg text-ink min-h-9 min-w-0 rounded-lg border px-2 text-xs"
const BUTTON = "border-hair hover:bg-hairsoft rounded-full border px-3 py-1 text-xs disabled:opacity-50"

function Select({ label, value, options, onChange }: { label: string; value: string; options: { value: string; label: string }[]; onChange: (value: string) => void }) {
  return (
    <label className="text-n600 flex min-w-0 flex-col gap-1.5 text-xs">
      {label}
      <select value={value} onChange={(event) => onChange(event.target.value)} className={SELECT}>
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
    </label>
  )
}

export function ExamplesPage() {
  const { t } = useTranslation("admin-examples")
  const [values, setValues] = useUrlState(DEFAULTS)
  const [search, setSearch] = useSearchParams()
  const taxonomy = useTaxonomy()
  const list = useExamples(values)
  const setStatus = useSetExampleStatus()
  const [sheet, setSheet] = useState<SheetMode | null>(null)
  const [preview, setPreview] = useState<string | null>(null)

  const collect = search.get("collect")
  const collectAsset = search.get("asset")
  // The trajectory page hands a session over in the URL; that opens the sheet without any state of its own.
  const mode: SheetMode | null = sheet ?? (collect ? { kind: "collect", sessionId: collect, assetId: collectAsset } : null)
  const closeSheet = () => {
    setSheet(null)
    if (collect) {
      const next = new URLSearchParams(search)
      next.delete("collect")
      next.delete("asset")
      setSearch(next, { replace: true })
    }
  }

  const all = { value: "", label: t("filters.all") }
  const entries = useMemo(() => taxonomy.data?.entries ?? [], [taxonomy.data])
  const industries = taxonomy.data?.industries ?? []
  const shopTypes = industries.find((item) => item.id === values.industry)?.shop_types ?? []

  const columns = useMemo<DataTableColumn<Example>[]>(
    () => [
      {
        key: "title",
        header: t("column.title"),
        render: (row) => (
          <div className="flex min-w-0 flex-col">
            <span className="text-ink truncate font-medium">{row.title}</span>
            {row.notes && <span className="text-n500 line-clamp-1 text-2xs">{row.notes}</span>}
          </div>
        ),
      },
      { key: "entry", header: t("column.entry"), render: (row) => entries.find((entry) => entry.id === row.entry_id)?.label ?? row.entry_id },
      { key: "industry", header: t("column.industry"), render: (row) => row.shop_type_label },
      {
        key: "tags",
        header: t("column.tags"),
        render: (row) => (
          <span className="flex flex-wrap gap-1">
            {row.tags.map((tag) => (
              <span key={tag} className="bg-hairsoft text-n700 rounded px-1.5 py-0.5 text-2xs">
                {tag}
              </span>
            ))}
          </span>
        ),
      },
      { key: "rating", header: t("column.rating"), className: "tabular-nums", render: (row) => `${row.rating}/5` },
      {
        key: "status",
        header: t("column.status"),
        render: (row) => <StatusPill tone={row.status === "active" ? "ok" : "muted"}>{t(`status.${row.status}`)}</StatusPill>,
      },
      { key: "use_count", header: t("column.useCount"), className: "tabular-nums", render: (row) => String(row.use_count) },
      { key: "updated", header: t("column.updatedAt"), render: (row) => (row.updated_at ? formatSince(row.updated_at) : "") },
      {
        key: "actions",
        header: t("column.actions"),
        render: (row) => (
          <span className="flex flex-wrap gap-1.5" onClick={(event) => event.stopPropagation()}>
            <button type="button" className={BUTTON} onClick={() => setPreview(preview === row.id ? null : row.id)}>
              {t("action.preview")}
            </button>
            <button type="button" className={BUTTON} onClick={() => setSheet({ kind: "edit", example: row })}>
              {t("action.edit")}
            </button>
            <button
              type="button"
              className={BUTTON}
              disabled={setStatus.isPending}
              onClick={() => setStatus.mutate({ id: row.id, status: row.status === "active" ? "disabled" : "active" })}
            >
              {t(row.status === "active" ? "action.disable" : "action.enable")}
            </button>
          </span>
        ),
      },
    ],
    [t, entries, preview, setStatus],
  )

  const rows = list.data?.items ?? []
  const total = list.data?.total ?? 0
  const offset = Number(values.offset) || 0
  const previewed = rows.find((row) => row.id === preview) ?? null

  return (
    <div className="flex flex-col gap-4">
      <form className="border-hair bg-card flex flex-wrap items-end gap-3 rounded-xl border p-4" aria-label={t("filters.label")} onSubmit={(event) => event.preventDefault()}>
        <Select label={t("filters.entry")} value={values.entry} options={[all, ...entries.map((entry) => ({ value: entry.id, label: entry.label }))]} onChange={(entry) => setValues({ entry })} />
        <Select
          label={t("filters.industry")}
          value={values.industry}
          options={[all, ...industries.map((industry) => ({ value: industry.id, label: industry.label }))]}
          onChange={(industry) => setValues({ industry, shopType: "" })}
        />
        <Select label={t("filters.shopType")} value={values.shopType} options={[all, ...shopTypes.map((shop) => ({ value: shop.id, label: shop.label }))]} onChange={(shopType) => setValues({ shopType })} />
        <Select
          label={t("filters.status")}
          value={values.status}
          options={[all, { value: "active", label: t("status.active") }, { value: "disabled", label: t("status.disabled") }]}
          onChange={(status) => setValues({ status })}
        />
        <label className="text-n600 flex min-w-0 flex-1 basis-48 flex-col gap-1.5 text-xs">
          {t("filters.keyword")}
          <input type="search" maxLength={80} placeholder={t("filters.placeholder")} value={values.q} onChange={(event) => setValues({ q: event.target.value })} className={`${SELECT} px-3`} />
        </label>
        <div className="ml-auto flex items-center gap-3">
          {list.data && <span className="text-n500 text-2xs">{t("list.total", { count: total })}</span>}
          <button type="button" className="bg-ink text-bg rounded-full px-3.5 py-1.5 text-xs font-medium" onClick={() => setSheet({ kind: "new" })}>
            {t("action.new")}
          </button>
        </div>
      </form>

      <section className="border-hair bg-card rounded-xl border p-4">
        <DataTable
          columns={columns}
          rows={rows}
          rowKey={(row) => row.id}
          isLoading={list.isLoading}
          error={list.error}
          emptyText={t("list.empty")}
          errorText={t("list.error")}
          loadingLabel={t("list.loading")}
          minWidth="min-w-[64rem]"
        />
        {total > PAGE_SIZE && (
          <div className="mt-3 flex items-center justify-end gap-2 text-xs">
            <button type="button" className={BUTTON} disabled={offset <= 0} onClick={() => setValues({ offset: String(Math.max(0, offset - PAGE_SIZE)) })}>
              ‹
            </button>
            <span className="text-n500 tabular-nums">
              {offset + 1}–{Math.min(total, offset + PAGE_SIZE)} / {total}
            </span>
            <button type="button" className={BUTTON} disabled={offset + PAGE_SIZE >= total} onClick={() => setValues({ offset: String(offset + PAGE_SIZE) })}>
              ›
            </button>
          </div>
        )}
      </section>

      {previewed && (
        <section className="border-hair bg-card flex flex-col gap-3 rounded-xl border p-4" aria-label={t("action.preview")} data-testid="example-preview">
          <h3 className="text-ink text-sm font-semibold">{previewed.title}</h3>
          {previewed.recipe && <pre className="text-n700 bg-bg border-hair max-h-64 overflow-auto rounded-lg border p-3 text-xs whitespace-pre-wrap">{previewed.recipe}</pre>}
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
            {previewed.outputs.map((asset) => (
              <AssetTile key={asset.asset_id} asset={asset} />
            ))}
          </div>
        </section>
      )}

      <ExampleSheet mode={mode} onClose={closeSheet} onSaved={() => toast("success", t("form.saved"))} />
    </div>
  )
}
