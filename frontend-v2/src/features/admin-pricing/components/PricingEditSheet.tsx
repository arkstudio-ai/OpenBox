// Edit one item's sale price (and, if wanted, its cost basis). The form is the
// full override: saving writes exactly what is on screen; reverting drops it.
import { useId } from "react"
import { useTranslation } from "react-i18next"
import { ApiError } from "@/shared/api/http"
import { Sheet } from "@/shared/ui/Sheet"
import { StatusPill } from "@/shared/ui/StatusPill"
import { formatCredits } from "@/shared/lib/format"
import { usePricingEditor, type PricingEditor } from "../hooks/usePricingEditor"
import { fieldValues } from "../lib"
import type { PricingItem } from "../types"
import { CostSection, FIELD_CLASS, FieldGroup, PreviewSection } from "./PricingEditSections"
import { PricingHistoryList } from "./PricingHistoryList"

function errorText(t: (key: string, options?: Record<string, unknown>) => string, error: unknown): string {
  if (error instanceof ApiError) {
    const fields = Array.isArray(error.meta.fields) ? (error.meta.fields as string[]).join(", ") : ""
    const text = t(`errors.${error.code}`, { defaultValue: t("errors.default"), fields, revision: error.meta.current_revision })
    return error.code === "BELOW_COST" ? `${text} ${t("editor.belowCostNext")}` : text
  }
  return t("errors.default")
}

function Footer({ item, editor }: { item: PricingItem; editor: PricingEditor }) {
  const { t } = useTranslation("admin-pricing")
  return (
    <div className="flex flex-wrap items-center justify-between gap-2">
      <div className="flex gap-3">
        {item.rule && (
          <button type="button" disabled={editor.busy} onClick={editor.doRevert} className="text-n700 text-xs underline">
            {t("editor.revert")}
          </button>
        )}
        {item.rule?.status !== "disabled" && (
          <button
            type="button"
            disabled={!editor.canDisable}
            onClick={() => editor.save("disabled")}
            className="text-dangerink text-xs underline disabled:opacity-50"
          >
            {t("editor.disable")}
          </button>
        )}
      </div>
      <button
        type="button"
        disabled={!editor.canSave}
        onClick={() => editor.save("active")}
        className="bg-ink text-bg rounded-lg px-4 py-2 text-sm font-medium disabled:opacity-50"
      >
        {editor.busy ? t("editor.saving") : t("editor.save")}
      </button>
    </div>
  )
}

function Check({
  checked,
  onChange,
  disabled,
  label,
}: {
  checked: boolean
  onChange: (next: boolean) => void
  disabled: boolean
  label: string
}) {
  return (
    <label className="text-n700 flex items-start gap-2 text-xs">
      <input type="checkbox" checked={checked} disabled={disabled} onChange={(e) => onChange(e.target.checked)} className="mt-0.5" />
      <span>{label}</span>
    </label>
  )
}

export function PricingEditSheet({ item, onClose }: { item: PricingItem; onClose: () => void }) {
  const { t } = useTranslation("admin-pricing")
  const id = useId()
  const editor = usePricingEditor(item)
  const baseSale = Object.entries(fieldValues(item.kind, item.base_sale))
    .map(([k, v]) => `${t(`fields.${k}`)} ${formatCredits(v)}`)
    .join(" · ")

  return (
    <Sheet
      open
      onClose={() => {
        if (!editor.busy) onClose()
      }}
      title={item.label}
      description={
        <span className="font-mono text-2xs">
          {item.key}
          {item.rule && ` · ${t("editor.revision", { revision: editor.revision })}`}
        </span>
      }
      closeLabel={t("editor.close")}
      footer={<Footer item={item} editor={editor} />}
    >
      <div className="flex flex-col gap-4 text-sm">
        {item.cost?.channel && <p className="text-n600 text-xs">{String(item.cost.channel)}</p>}
        <div className="flex flex-wrap gap-1">
          {item.flags.includes("below_cost") && <StatusPill tone="danger">{t("flags.below_cost")}</StatusPill>}
          {item.rule?.status === "disabled" && <StatusPill tone="danger">{t("flags.disabled")}</StatusPill>}
          {item.rule && item.rule.status !== "disabled" && <StatusPill tone="accent">{t("flags.overridden")}</StatusPill>}
        </div>

        <section className="space-y-2">
          <h3 className="text-ink text-xs font-medium">{t("editor.sale")}</h3>
          <FieldGroup
            kind={item.kind}
            idPrefix={`${id}-sale`}
            values={editor.sale}
            onChange={editor.setSale}
            disabled={editor.busy}
            unit={item.unit}
          />
          {baseSale && (
            <p className="text-n500 text-2xs">
              {t("editor.baseSale")}
              {baseSale}
            </p>
          )}
        </section>

        <CostSection item={item} editor={editor} id={id} />
        <PreviewSection item={item} editor={editor} />

        <label htmlFor={`${id}-until`} className="text-n700 text-xs">
          {t("editor.validUntil")}
          <input
            id={`${id}-until`}
            type="datetime-local"
            value={editor.validUntil}
            disabled={editor.busy}
            onChange={(e) => editor.setValidUntil(e.target.value)}
            className={`${FIELD_CLASS} mt-1 font-sans`}
          />
        </label>

        <label htmlFor={`${id}-reason`} className="text-n700 text-xs">
          {t("editor.reason")}
          <textarea
            id={`${id}-reason`}
            rows={2}
            maxLength={1000}
            value={editor.reason}
            disabled={editor.busy}
            onChange={(e) => editor.setReason(e.target.value)}
            className={`${FIELD_CLASS} mt-1 font-sans`}
          />
        </label>

        <Check checked={editor.allowBelowCost} onChange={editor.setAllowBelowCost} disabled={editor.busy} label={t("editor.allowBelowCost")} />
        <Check checked={editor.confirmDisable} onChange={editor.setConfirmDisable} disabled={editor.busy} label={t("editor.confirmDisable")} />

        {editor.error != null && (
          <p role="alert" className="text-dangerink bg-dangersoft rounded-lg px-3 py-2 text-xs">
            {errorText(t, editor.error)}
          </p>
        )}

        <PricingHistoryList itemKey={item.key} />
      </div>
    </Sheet>
  )
}
