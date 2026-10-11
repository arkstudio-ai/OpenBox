// The table's columns. Everything an operator compares is on one row: what
// the item costs us, what we charge, the margin, and whether anyone used it.
import { useTranslation } from "react-i18next"
import type { DataTableColumn } from "@/shared/ui/DataTable"
import { StatusPill } from "@/shared/ui/StatusPill"
import { formatCredits, formatDay } from "@/shared/lib/format"
import { BADGE_FLAGS, DASH, FLAG_TONES, FIELDS, formatMargin, marginTone } from "../lib"
import type { PricingItem } from "../types"
import { PriceLines } from "./PriceLines"

const MARGIN_CLASS = {
  danger: "text-dangerink",
  warn: "text-n800",
  ok: "text-s800",
  muted: "text-n500",
} as const

export function usePricingColumns(onEdit: (item: PricingItem) => void): DataTableColumn<PricingItem>[] {
  const { t } = useTranslation("admin-pricing")
  return [
    {
      key: "label",
      header: t("columns.item"),
      className: "max-w-[18rem]",
      render: (item) => (
        <div className="min-w-0">
          <p className="text-ink truncate text-sm font-medium" title={item.label}>
            {item.label}
          </p>
          <p className="text-n500 font-mono text-2xs truncate" title={item.key}>
            {item.key}
          </p>
          {item.alias_of && !item.rule?.sale && (
            <p className="text-n500 text-2xs">{t("table.alias", { target: item.alias_of })}</p>
          )}
          {(item.cost?.channel || item.channel) && (
            <p className="text-n500 text-2xs truncate" title={String(item.cost?.channel ?? item.channel)}>
              {String(item.cost?.channel ?? item.channel)}
            </p>
          )}
        </div>
      ),
    },
    {
      key: "cost",
      header: t("columns.cost"),
      render: (item) => (
        <div className="space-y-1">
          <PriceLines kind={item.kind} values={item.cost_credits} unit={item.unit} />
          {item.cost?.basis && <p className="text-n500 text-2xs">{t("table.costBasis", { basis: item.cost.basis })}</p>}
        </div>
      ),
    },
    {
      key: "sale",
      header: t("columns.sale"),
      render: (item) => (
        <div className="space-y-1">
          <PriceLines kind={item.kind} values={item.sale_credits} unit={item.unit} highlight={item.below_cost_fields} />
          {item.rule?.sale && item.base_sale && (
            <p className="text-n500 text-2xs">
              {t("table.base")}{" "}
              {FIELDS[item.kind]
                .map((field) => {
                  const base = item.kind === "voice-realtime" ? item.base_sale?.per_million?.[field] : item.base_sale?.[field]
                  return typeof base === "string" ? formatCredits(base) : null
                })
                .filter(Boolean)
                .join(" / ")}
            </p>
          )}
        </div>
      ),
    },
    {
      key: "margin",
      header: t("columns.margin"),
      render: (item) => {
        const fields = FIELDS[item.kind].filter((field) => item.sale_credits[field] !== undefined)
        if (fields.length === 0) return <span className="text-n500">{DASH}</span>
        return (
          <ul className="space-y-0.5 text-xs leading-4">
            {fields.map((field) => (
              <li key={field} className={`font-mono tabular-nums ${MARGIN_CLASS[marginTone(item.margin_pct[field])]}`}>
                {formatMargin(item.margin_pct[field])}
              </li>
            ))}
          </ul>
        )
      },
    },
    {
      key: "usage",
      header: t("columns.usage"),
      render: (item) => {
        const usage = item.usage_30d
        if (!usage || usage.events === 0) return <span className="text-n500">{DASH}</span>
        return (
          <dl className="grid grid-cols-[auto_1fr] gap-x-2 text-2xs leading-4">
            <dt className="text-n500">{t("table.events")}</dt>
            <dd className="font-mono tabular-nums">{usage.events}</dd>
            <dt className="text-n500">{t("table.revenue")}</dt>
            <dd className="font-mono tabular-nums">{formatCredits(usage.credits)}</dd>
            <dt className="text-n500">{t("table.spend")}</dt>
            <dd className="font-mono tabular-nums">{usage.costed_events ? formatCredits(usage.cost_credits) : DASH}</dd>
            <dt className="text-n500">{t("table.gross")}</dt>
            <dd className={`font-mono tabular-nums ${Number(usage.gross_margin) < 0 ? "text-dangerink" : ""}`}>
              {usage.gross_margin == null ? DASH : formatCredits(usage.gross_margin)}
            </dd>
          </dl>
        )
      },
    },
    {
      key: "flags",
      header: t("columns.status"),
      render: (item) => (
        <div className="flex max-w-[12rem] flex-wrap gap-1">
          {BADGE_FLAGS.filter((flag) => item.flags.includes(flag)).map((flag) => (
            <StatusPill
              key={flag}
              tone={FLAG_TONES[flag]}
              title={flag === "expiring" && item.expires_at ? formatDay(item.expires_at) : undefined}
            >
              {t(`flags.${flag}`)}
            </StatusPill>
          ))}
        </div>
      ),
    },
    {
      key: "actions",
      header: t("columns.actions"),
      render: (item) => (
        <button
          type="button"
          className="text-ink text-xs whitespace-nowrap underline"
          onClick={(event) => {
            event.stopPropagation()
            onEdit(item)
          }}
        >
          {t("table.edit")}
        </button>
      ),
    },
  ]
}
