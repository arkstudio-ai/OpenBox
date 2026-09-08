// Usage over the server's fixed window, one tile per settlement status —
// "charged" and "shadow" separate real spend from what enforcement would have
// cost, which is the question this card exists to answer.
import { useTranslation } from "react-i18next"
import { formatCredits, formatNumber } from "@/shared/lib/format"
import { formatWhen } from "@/features/admin-billing/lib/display"
import type { WorkspaceBillingDetail } from "@/features/admin-billing/types"
import { SectionCard } from "./SectionCard"

export function WorkspaceUsage({ usage }: { usage: WorkspaceBillingDetail["usage"] }) {
  const { t } = useTranslation("admin-billing")

  return (
    <SectionCard
      title={t("workspace.usage.title", { days: usage.days })}
      hint={t("workspace.usage.since", { time: formatWhen(usage.since) })}
    >
      {usage.items.length === 0 ? (
        <p className="py-6 text-center text-sm text-n600">
          {t("workspace.usage.empty", { days: usage.days })}
        </p>
      ) : (
        <ul className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
          {usage.items.map((item) => (
            <li key={item.status} className="rounded-lg bg-bg px-3 py-2.5">
              <p className="text-2xs text-n500">
                {/* A new settlement status must read as itself, not as a key. */}
                {t(`usageStatus.${item.status}`, { defaultValue: item.status })}
              </p>
              <p className="mt-1 text-sm tabular-nums text-ink">
                {t("workspace.usage.events", { value: formatNumber(item.events) })}
              </p>
              <p className="mt-0.5 text-2xs tabular-nums text-n600">
                {formatNumber(item.total_tokens)} {t("workspace.usage.tokens")}
              </p>
              <p className="text-2xs tabular-nums text-n600">
                {formatCredits(item.credits)} {t("workspace.usage.credits")}
              </p>
            </li>
          ))}
        </ul>
      )}
    </SectionCard>
  )
}
