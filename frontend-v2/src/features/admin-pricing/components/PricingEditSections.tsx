// The sheet's building blocks: a grid of price inputs, the cost block and the preview block.
import { useTranslation } from "react-i18next"
import { formatCredits } from "@/shared/lib/format"
import type { PricingEditor } from "../hooks/usePricingEditor"
import { DASH, FIELDS, isDecimal, SAMPLE_USAGE } from "../lib"
import type { PricingItem, PricingKind } from "../types"

export const FIELD_CLASS =
  "w-full rounded-lg border border-hair bg-bg px-3 py-1.5 font-mono text-sm text-ink disabled:opacity-60"

export function FieldGroup({
  kind,
  idPrefix,
  values,
  onChange,
  disabled,
  unit,
}: {
  kind: PricingKind
  idPrefix: string
  values: Record<string, string>
  onChange: (next: Record<string, string>) => void
  disabled: boolean
  unit: string
}) {
  const { t } = useTranslation("admin-pricing")
  return (
    <div className="grid grid-cols-2 gap-2">
      {FIELDS[kind].map((field) => {
        const value = values[field] ?? ""
        const bad = value.trim() !== "" && !isDecimal(value)
        return (
          <label key={field} htmlFor={`${idPrefix}-${field}`} className="text-n700 text-xs">
            {t(`fields.${field}`)} <span className="text-n500">{t(`units.${unit}`)}</span>
            <input
              id={`${idPrefix}-${field}`}
              inputMode="decimal"
              autoComplete="off"
              disabled={disabled}
              value={value}
              aria-invalid={bad || undefined}
              onChange={(event) => onChange({ ...values, [field]: event.target.value })}
              className={`${FIELD_CLASS} mt-1 ${bad ? "border-danger" : ""}`}
            />
          </label>
        )
      })}
    </div>
  )
}

function summarize(t: (key: string) => string, values: Record<string, string>): string {
  return Object.entries(values)
    .map(([k, v]) => `${t(`fields.${k}`)} ${formatCredits(v)}`)
    .join(" · ")
}

export function CostSection({ item, editor, id }: { item: PricingItem; editor: PricingEditor; id: string }) {
  const { t } = useTranslation("admin-pricing")
  const current = Object.keys(item.cost_credits).length ? summarize(t, item.cost_credits) : t("editor.noCost")
  return (
    <section className="space-y-2">
      <div className="flex items-center justify-between">
        <h3 className="text-ink text-xs font-medium">{t("editor.cost")}</h3>
        <label className="text-n600 flex items-center gap-1.5 text-xs">
          <input
            type="checkbox"
            checked={editor.editCost}
            onChange={(event) => editor.setEditCost(event.target.checked)}
            disabled={editor.busy}
          />
          {t("editor.editCost")}
        </label>
      </div>
      {editor.editCost ? (
        <>
          <FieldGroup
            kind={item.kind}
            idPrefix={`${id}-cost`}
            values={editor.cost}
            onChange={editor.setCost}
            disabled={editor.busy}
            unit={item.unit}
          />
          <div className="grid grid-cols-2 gap-2">
            <label htmlFor={`${id}-basis`} className="text-n700 text-xs">
              {t("editor.costBasis")}
              <input
                id={`${id}-basis`}
                value={editor.basis}
                maxLength={120}
                disabled={editor.busy}
                onChange={(e) => editor.setBasis(e.target.value)}
                className={`${FIELD_CLASS} mt-1 font-sans`}
              />
            </label>
            <label htmlFor={`${id}-source`} className="text-n700 text-xs">
              {t("editor.costSource")}
              <input
                id={`${id}-source`}
                value={editor.source}
                maxLength={500}
                disabled={editor.busy}
                onChange={(e) => editor.setSource(e.target.value)}
                className={`${FIELD_CLASS} mt-1 font-sans`}
              />
            </label>
          </div>
        </>
      ) : (
        <p className="text-n600 text-xs">
          {current}
          {item.cost?.basis && ` · ${t("table.costBasis", { basis: item.cost.basis })}`}
          {item.cost?.verified_at && ` · ${item.cost.verified_at}`}
        </p>
      )}
    </section>
  )
}

function Money({ value }: { value: string | null | undefined }) {
  return <dd className="font-mono tabular-nums">{value == null ? DASH : formatCredits(value)}</dd>
}

export function PreviewSection({ item, editor }: { item: PricingItem; editor: PricingEditor }) {
  const { t } = useTranslation("admin-pricing")
  const usage = Object.entries(SAMPLE_USAGE[item.kind])
    .map(([k, v]) => `${t(`fields.${k}`, { defaultValue: k })} ${v.toLocaleString()}`)
    .join(" · ")
  const data = editor.preview.data
  return (
    <section className="space-y-2">
      <div className="flex items-center justify-between">
        <h3 className="text-ink text-xs font-medium">{t("editor.preview")}</h3>
        <button
          type="button"
          onClick={editor.runPreview}
          disabled={editor.preview.isPending}
          className="text-ink text-xs underline"
        >
          {t("editor.runPreview")}
        </button>
      </div>
      <p className="text-n500 text-2xs">{t("editor.previewHint", { usage })}</p>
      {data && (
        <dl className="grid grid-cols-[auto_1fr_1fr] gap-x-3 gap-y-1 text-xs">
          <dt />
          <dd className="text-n500">{t("editor.credits")}</dd>
          <dd className="text-n500">{t("editor.costCredits")}</dd>
          <dt className="text-n600">{t("editor.current")}</dt>
          <Money value={data.current.credits} />
          <Money value={data.current.cost_credits} />
          {data.draft && (
            <>
              <dt className="text-n600">{t("editor.draft")}</dt>
              <Money value={data.draft.credits} />
              <Money value={data.draft.cost_credits} />
            </>
          )}
        </dl>
      )}
    </section>
  )
}
