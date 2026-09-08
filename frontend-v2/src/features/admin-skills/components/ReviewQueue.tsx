import { useTranslation } from "react-i18next"
import { Spinner } from "@/shared/ui/Spinner"
import { Pagination } from "@/shared/ui/Pagination"
import { cn } from "@/shared/lib/cn"
import { formatBytes, formatDateTime } from "@/shared/lib/format"
import type { StoreEntry } from "@/features/admin-skills/types"
import { FilterPills } from "./FilterPills"

const STATES = ["pending", "rejected"] as const

interface Props {
  state: string
  onState: (state: string) => void
  rows: readonly StoreEntry[]
  isLoading: boolean
  error: unknown
  selectedId: string
  onSelect: (catalogId: string) => void
  offset: number
  limit: number
  total: number
  onOffsetChange: (offset: number) => void
}

/** The submission queue: pick one on the left, read it on the right. */
export function ReviewQueue(props: Props) {
  const { t } = useTranslation("admin-skills")
  const { rows, isLoading, error, selectedId, onSelect, offset, limit, total } = props

  return (
    <section className="border-hair bg-card flex w-full flex-col gap-3 rounded-xl border p-4 lg:w-80 lg:flex-none">
      <FilterPills
        label={t("review.filter")}
        value={props.state}
        onChange={props.onState}
        options={STATES.map((value) => ({ value, label: t(`review.state.${value}`) }))}
      />

      {isLoading ? (
        <div className="flex justify-center py-10">
          <Spinner className="size-5" />
          <span className="sr-only">{t("list.loading")}</span>
        </div>
      ) : error ? (
        <p role="alert" className="text-danger py-8 text-center text-sm">
          {t("list.error")}
        </p>
      ) : rows.length === 0 ? (
        <p className="text-n600 py-8 text-center text-sm">
          {t(props.state === "rejected" ? "review.emptyRejected" : "review.emptyPending")}
        </p>
      ) : (
        <ul className="flex flex-col gap-1">
          {rows.map((entry) => (
            <li key={entry.catalog_id}>
              <button
                type="button"
                aria-current={entry.catalog_id === selectedId ? "true" : undefined}
                onClick={() => onSelect(entry.catalog_id)}
                className={cn(
                  "w-full rounded-lg px-2.5 py-2 text-start",
                  entry.catalog_id === selectedId ? "bg-n200 text-ink" : "text-n800 hover:bg-hairsoft",
                )}
              >
                <div className="truncate text-xs font-medium">{entry.title}</div>
                <div className="text-n600 text-2xs mt-0.5">
                  {entry.author?.username ?? entry.publisher ?? "—"}
                  {entry.size ? ` · ${formatBytes(entry.size)}` : ""}
                </div>
                {entry.published_at && (
                  <div className="text-n600 text-2xs">
                    {t("review.submittedAt", { time: formatDateTime(entry.published_at) })}
                  </div>
                )}
              </button>
            </li>
          ))}
        </ul>
      )}

      {total > 0 && (
        <Pagination
          offset={offset}
          limit={limit}
          total={total}
          onOffsetChange={props.onOffsetChange}
          labels={{
            previous: t("list.previous"),
            next: t("list.next"),
            nav: t("review.queueNav"),
            range: t("list.range", { from: offset + 1, to: Math.min(offset + limit, total), total }),
          }}
        />
      )}
    </section>
  )
}
