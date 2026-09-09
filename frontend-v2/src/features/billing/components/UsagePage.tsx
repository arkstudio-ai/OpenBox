import { useState } from "react"
import { useTranslation } from "react-i18next"
import { Link } from "react-router"
import { ChevronLeft, ChevronRight } from "lucide-react"
import { useBillingSummary, useCreditBalance, useUsageEvents } from "@/shared/api/billing"
import type { UsageDateFilter, UsageEntry } from "@/shared/api/billing"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import { Spinner } from "@/shared/ui/Spinner"
import { formatCredits, formatDateTime, formatNumber, formatTokens } from "@/shared/lib/format"
import { paths } from "@/shared/router/paths"
import { CreditBalanceCard } from "./CreditBalanceCard"
import { UsageFilters } from "./UsageFilters"

function UsageRow({ entry }: { entry: UsageEntry }) {
  const { t } = useTranslation("billing")
  return (
    <li className="border-hair flex flex-col gap-2 border-t px-4 py-4 first:border-t-0">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <span className="bg-n200 text-ink min-w-0 rounded-md px-2 py-1 font-mono text-xs break-all">
          {entry.model_id.split("/").at(-1)}
        </span>
        <span
          className="text-a700 ms-auto flex-none text-sm font-medium tabular-nums"
          title={entry.credits ?? t("usage.status.unpriced")}
        >
          {entry.credits == null
            ? t(`usage.status.${entry.status}`)
            : t("usage.points", { value: formatCredits(entry.credits) })}
        </span>
      </div>
      {entry.session_available ? (
        <Link className="line-clamp-2 text-sm hover:underline" to={paths.chat(entry.session_id)}>
          {entry.session_title}
        </Link>
      ) : (
        <span className="line-clamp-2 text-sm">{entry.session_title}</span>
      )}
      <div className="text-n600 flex flex-wrap gap-x-3 gap-y-1 text-xs">
        {entry.kind.startsWith("video_") ? (
          <>
            {entry.tokens.duration_sec != null && (
              <span>{t("usage.media.duration", { value: formatNumber(Math.round(entry.tokens.duration_sec * 10) / 10) })}</span>
            )}
            {entry.tokens.minutes_billed != null && (
              <span>{t("usage.media.minutesBilled", { value: formatNumber(entry.tokens.minutes_billed) })}</span>
            )}
            {entry.tokens.seconds_billed != null && (
              <span>{t("usage.media.secondsBilled", { value: formatNumber(entry.tokens.seconds_billed) })}</span>
            )}
            {entry.tokens.tier && <span>{t("usage.media.tier", { value: entry.tokens.tier })}</span>}
            {entry.tokens.resolution && <span>{t("usage.media.resolution", { value: entry.tokens.resolution })}</span>}
          </>
        ) : (
          <>
            <span>{t("usage.input", { value: formatNumber(entry.tokens.input ?? 0) })}</span>
            <span>{t("usage.output", { value: formatNumber(entry.tokens.output ?? 0) })}</span>
            <span>{t("usage.cache", { value: formatNumber(entry.tokens.cache ?? 0) })}</span>
          </>
        )}
      </div>
      <div className="text-2xs text-n600 flex flex-wrap items-center justify-between gap-1">
        <time dateTime={entry.created_at}>{formatDateTime(entry.created_at)}</time>
        <span>
          {t(`usage.kind.${entry.kind}`, { defaultValue: entry.kind })} · {t(`usage.status.${entry.status}`)}
        </span>
      </div>
    </li>
  )
}

function UsageContent() {
  const { t } = useTranslation("billing")
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)
  const [dates, setDates] = useState<UsageDateFilter>({})
  const filtered = Boolean(dates.date_from || dates.date_to)
  const summary = useBillingSummary(dates)
  const balance = useCreditBalance()
  const usage = useUsageEvents(page, pageSize, dates)
  const pages = usage.data?.total_pages ?? 1

  return (
    <div className="flex min-w-0 flex-col gap-5">
      <CreditBalanceCard balance={balance.data?.balance}>
        <div className="border-hair grid grid-cols-2 gap-3 border-t pt-4">
          <div className="flex min-w-0 flex-col gap-1">
            <span className="text-n600 text-xs">
              {t(filtered ? "usage.filteredTokens" : "usage.totalTokens")}
            </span>
            <span className="text-2xl break-all tabular-nums">
              {summary.data ? formatTokens(summary.data.total_tokens) : "—"}
            </span>
          </div>
          <div className="flex min-w-0 flex-col gap-1">
            <span className="text-n600 text-xs">
              {t(filtered ? "usage.filteredCredits" : "usage.totalCredits")}
            </span>
            <span className="text-2xl break-all tabular-nums">
              {formatCredits(summary.data?.total_credits)}
            </span>
          </div>
        </div>
      </CreditBalanceCard>

      {(summary.isError || balance.isError || usage.isError) && (
        <div role="alert" className="flex items-center justify-between gap-2 text-sm">
          <span>{t("usage.loadError")}</span>
          <button
            type="button"
            className="bg-n200 hover:bg-n300 rounded-full px-3 py-1.5"
            onClick={() => {
              void summary.refetch()
              void balance.refetch()
              void usage.refetch()
            }}
          >
            {t("usage.retry")}
          </button>
        </div>
      )}

      <section className="flex min-w-0 flex-col gap-3" aria-label={t("usage.details")}>
        <UsageFilters
          onApply={(value) => {
            setDates(value)
            setPage(1)
          }}
        />
        <div className="text-n600 flex flex-wrap items-center justify-between gap-2 text-xs">
          <span>{t("usage.detailsCount", { count: usage.data?.total ?? 0 })}</span>
          <label className="flex items-center gap-1.5">
            {t("usage.pageSize")}
            <select
              className="border-hair bg-card text-ink rounded-md border px-2 py-1"
              value={pageSize}
              onChange={(event) => {
                setPageSize(Number(event.target.value))
                setPage(1)
              }}
            >
              {[10, 20, 50].map((size) => (
                <option key={size} value={size}>
                  {size}
                </option>
              ))}
            </select>
          </label>
        </div>
        <nav className="flex items-center justify-between gap-2 text-xs" aria-label={t("usage.pagination")}>
          <button
            type="button"
            disabled={page <= 1 || usage.isFetching}
            className="border-hair hover:bg-n200 flex items-center gap-1 rounded-full border px-3 py-2 disabled:opacity-40"
            onClick={() => setPage((value) => value - 1)}
          >
            <ChevronLeft className="size-3.5" />
            {t("usage.previous")}
          </button>
          <span className="text-n600" aria-live="polite">
            {t("usage.page", { page, pages })}
          </span>
          <button
            type="button"
            disabled={page >= pages || usage.isFetching}
            className="border-hair hover:bg-n200 flex items-center gap-1 rounded-full border px-3 py-2 disabled:opacity-40"
            onClick={() => setPage((value) => value + 1)}
          >
            {t("usage.next")}
            <ChevronRight className="size-3.5" />
          </button>
        </nav>
        {usage.isLoading ? (
          <div className="flex justify-center py-10">
            <Spinner className="size-5" />
          </div>
        ) : usage.data?.items.length ? (
          <ul className="border-hair bg-card min-w-0 rounded-xl border">
            {usage.data.items.map((entry) => (
              <UsageRow key={entry.id} entry={entry} />
            ))}
          </ul>
        ) : (
          !usage.isError && (
            <p className="border-hair bg-card text-n600 rounded-xl border px-5 py-8 text-center text-sm">
              {t(filtered ? "usage.noMatches" : "usage.empty")}
            </p>
          )
        )}
      </section>
    </div>
  )
}

export function UsagePage() {
  const workspaceId = useWorkspaceStore((s) => s.currentId)
  return <UsageContent key={workspaceId} />
}
