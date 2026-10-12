// Small shared pieces of the lite pages: status pill, tool chips, a paging nav
// and the time/token formatters that read the way an operator scans a list.
import { useTranslation } from "react-i18next"
import { formatCredits, formatSince, formatTokens } from "@/shared/lib/format"
import { StatusPill } from "@/shared/ui/StatusPill"
import type { TimeMode } from "../../stores/view"
import { formatDuration, formatInstant } from "../../utils/time"
import { resolveTool, turnStatusLabel, turnStatusTone } from "../labels"
import type { LiteLabels, ToolCount } from "../types"

export const BUTTON =
  "border-hair hover:bg-hairsoft inline-flex items-center gap-1.5 rounded-full border px-3 py-1.5 text-xs disabled:opacity-40"
export const FIELD =
  "border-hair bg-bg text-ink placeholder:text-n500 min-w-0 rounded-lg border px-2.5 py-1.5 text-sm"
const LOCAL: TimeMode = "local"

export function TurnStatus({ status, reason }: { status: string; reason?: string | null }) {
  const { t } = useTranslation("admin-trajectories")
  return (
    <StatusPill tone={turnStatusTone(status)} title={reason ?? undefined}>
      {turnStatusLabel(status, t)}
    </StatusPill>
  )
}

/** "3 分钟前", with the exact instant for anyone who hovers. */
export function Since({ iso }: { iso: string | null | undefined }) {
  const { t, i18n } = useTranslation("admin-trajectories")
  if (!iso) return <span className="text-n500">{t("common.notRecorded")}</span>
  return (
    <time dateTime={iso} title={formatInstant(iso, LOCAL, i18n.language) ?? iso}>
      {formatSince(iso)}
    </time>
  )
}

/** "14:32" today, "10-11 09:40" on another day, the exact instant on hover. */
export function Clock({ iso }: { iso: string | null | undefined }) {
  const { t, i18n } = useTranslation("admin-trajectories")
  if (!iso) return <span className="text-n500">{t("common.notRecorded")}</span>
  const date = new Date(iso)
  const today = new Date()
  const sameDay = date.toDateString() === today.toDateString()
  const text = new Intl.DateTimeFormat(i18n.language, {
    ...(sameDay ? {} : { month: "2-digit", day: "2-digit" }),
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).format(date)
  return (
    <time dateTime={iso} title={formatInstant(iso, LOCAL, i18n.language) ?? iso}>
      {text}
    </time>
  )
}

export function Duration({ ms }: { ms: number | null | undefined }) {
  const { t, i18n } = useTranslation("admin-trajectories")
  const text = formatDuration(ms, i18n.language)
  return <span>{text ?? t("common.dash")}</span>
}

export function Tokens({
  input,
  output,
  compact = false,
}: {
  input: number | null
  output: number | null
  compact?: boolean
}) {
  const { t } = useTranslation("admin-trajectories")
  if (input === null || output === null) return <span className="text-n500">{t("common.notRecorded")}</span>
  if (compact)
    return (
      <span title={t("list.tokens", { input: formatTokens(input), output: formatTokens(output) })}>
        {formatTokens(input + output)}
      </span>
    )
  return <span>{t("list.tokens", { input: formatTokens(input), output: formatTokens(output) })}</span>
}

/** Credits charged, with the token counts for anyone who hovers; "未计费" when nothing was charged. */
export function Credits({
  value,
  input,
  output,
}: {
  value: string | null | undefined
  input?: number | null
  output?: number | null
}) {
  const { t } = useTranslation("admin-trajectories")
  const tokens =
    input != null && output != null
      ? t("list.tokens", { input: formatTokens(input), output: formatTokens(output) })
      : undefined
  if (value == null)
    return (
      <span className="text-n500" title={tokens}>
        {t("lite.credits.none")}
      </span>
    )
  return <span title={tokens}>{formatCredits(value)}</span>
}

const DOT = "inline-block size-1.5 flex-none rounded-full"

/** What the AI did in a turn: tool actions by count, failed ones marked, the long tail folded into "+N". */
export function ToolChips({
  counts,
  labels,
  max = 3,
}: {
  counts: Record<string, ToolCount>
  labels: LiteLabels
  max?: number
}) {
  const { t } = useTranslation("admin-trajectories")
  const entries = Object.entries(counts).sort((a, b) => b[1].failed - a[1].failed || b[1].total - a[1].total)
  if (!entries.length) return <span className="text-n500 text-xs">{t("lite.turns.noTools")}</span>
  const shown = entries.slice(0, max)
  const rest = entries.length - shown.length
  return (
    <span className="flex flex-wrap gap-1.5">
      {shown.map(([id, count]) => (
        <span
          key={id}
          title={id}
          className={
            count.failed
              ? "bg-dangersoft text-dangerink text-2xs inline-flex items-center gap-1.5 rounded-md px-2 py-0.5 font-medium"
              : "bg-hairsoft text-n800 text-2xs inline-flex items-center gap-1.5 rounded-md px-2 py-0.5"
          }
        >
          {count.failed ? <span aria-hidden className={`${DOT} bg-dangerink`} /> : null}
          {t("lite.turns.toolCount", { label: resolveTool(id, labels).label, count: count.total })}
        </span>
      ))}
      {rest > 0 && <span className="text-n500 text-2xs">+{rest}</span>}
    </span>
  )
}

export function Paging({
  page,
  hasPrevious,
  hasNext,
  onPrevious,
  onNext,
}: {
  page: number
  hasPrevious: boolean
  hasNext: boolean
  onPrevious: () => void
  onNext: () => void
}) {
  const { t } = useTranslation("admin-trajectories")
  return (
    <nav
      aria-label={t("list.nav")}
      className="text-n600 flex flex-wrap items-center justify-end gap-2 pt-1 text-xs"
    >
      <span>{t("list.page", { page })}</span>
      <button type="button" className={BUTTON} disabled={!hasPrevious} onClick={onPrevious}>
        {t("list.previous")}
      </button>
      <button type="button" className={BUTTON} disabled={!hasNext} onClick={onNext}>
        {t("list.next")}
      </button>
    </nav>
  )
}
