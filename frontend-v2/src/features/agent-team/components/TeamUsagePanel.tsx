import { useTranslation } from "react-i18next"
import { Spinner } from "@/shared/ui/Spinner"
import { useTeamUsage } from "../api/teams"
import type { TeamSnapshot, TeamUsage } from "../types"

export function UsageSummary({ usage }: { usage: TeamUsage }) {
  const { t } = useTranslation("teams")
  return (
    <div className="text-n600 space-y-1 text-xs">
      <p>
        {t("usageValue", {
          credits: Number(usage.credits).toLocaleString(undefined, { maximumFractionDigits: 8 }),
          tokens: usage.tokens.toLocaleString(),
        })}
      </p>
      {(usage.unpriced > 0 || usage.pending > 0) && (
        <p className="text-a700">{t("usageIncomplete", { count: usage.unpriced + usage.pending })}</p>
      )}
    </div>
  )
}

export function TeamUsagePanel({ team }: { team: TeamSnapshot }) {
  const { t } = useTranslation("teams")
  const query = useTeamUsage(team.id)
  if (query.isLoading) return <Spinner className="size-5" />
  if (query.error)
    return (
      <p role="alert" className="text-danger text-sm">
        {query.error.message}
      </p>
    )
  if (!query.data) return null
  return (
    <div className="space-y-3">
      <UsageSummary usage={query.data} />
      <p className="text-n600 text-xs">{t("usageSource")}</p>
      <div className="grid gap-2 sm:grid-cols-2">
        {query.data.categories
          ?.filter((item) => item.category !== "unattributed" || item.calls > 0)
          .map((item) => (
            <article key={item.category} className="border-hair bg-card space-y-2 rounded-xl border p-3">
              <strong className="text-sm">{t(`costCategory.${item.category}`)}</strong>
              <UsageSummary usage={item} />
            </article>
          ))}
      </div>
      <p className="text-n600 text-xs">{t("costCategoryHint")}</p>
      {query.data.items?.map((item) => (
        <article
          key={`${item.member_id}:${item.model}:${item.kind}:${item.category}`}
          className="border-hair bg-card space-y-2 rounded-xl border p-3"
        >
          <strong className="text-sm">
            {team.members.find((member) => member.id === item.member_id)?.name}
          </strong>
          <p className="text-n600 text-xs break-all">
            {item.model} · {item.kind} · {t(`costCategory.${item.category}`)}
          </p>
          <UsageSummary usage={item} />
        </article>
      ))}
    </div>
  )
}
