// Every revision an item ever had, newest first: who, when, why, and the price.
import { useTranslation } from "react-i18next"
import { formatCredits, formatDateTime } from "@/shared/lib/format"
import { usePricingHistory } from "../api"
import type { PricingRule } from "../types"

function priceSummary(rule: PricingRule, t: (key: string) => string): string {
  if (rule.status === "disabled") return t("flags.disabled")
  const sale = rule.sale ?? {}
  const per = (sale.per_million as Record<string, string> | undefined) ?? sale
  return Object.entries(per)
    .filter(([, v]) => typeof v === "string")
    .map(([k, v]) => `${t(`fields.${k}`)} ${formatCredits(v as string)}`)
    .join(" · ")
}

export function PricingHistoryList({ itemKey }: { itemKey: string }) {
  const { t } = useTranslation("admin-pricing")
  const history = usePricingHistory(itemKey)
  const actors = new Map(history.data?.operations.map((op) => [op.actor.id, op.actor.username ?? op.actor.id]) ?? [])
  return (
    <section className="space-y-2">
      <h3 className="text-ink text-xs font-medium">{t("editor.history")}</h3>
      {history.isLoading && <p className="text-n500 text-xs">{t("table.loading")}</p>}
      {history.data && history.data.revisions.length === 0 && <p className="text-n500 text-xs">{t("editor.noHistory")}</p>}
      {history.data && history.data.revisions.length > 0 && (
        <ol className="divide-hair divide-y text-xs">
          {history.data.revisions.map((rule) => (
            <li key={rule.id} className="space-y-0.5 py-2">
              <div className="flex flex-wrap items-baseline justify-between gap-2">
                <span className="font-medium">
                  {t("editor.revision", { revision: rule.revision })}
                  {rule.superseded_at == null && ` · ${t("editor.current")}`}
                </span>
                <span className="text-n500 text-2xs">
                  {actors.get(rule.actor_user_id) ?? rule.actor_user_id} · {formatDateTime(rule.created_at)}
                </span>
              </div>
              <p className="font-mono tabular-nums">{priceSummary(rule, t)}</p>
              <p className="text-n600">{rule.reason}</p>
              {rule.valid_until && (
                <p className="text-n500 text-2xs">{t("editor.until", { at: formatDateTime(rule.valid_until) })}</p>
              )}
            </li>
          ))}
        </ol>
      )}
    </section>
  )
}
