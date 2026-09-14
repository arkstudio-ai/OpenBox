import { useTranslation } from "react-i18next"
import { ChevronRight } from "lucide-react"
import { cn } from "@/shared/lib/cn"
import { labelKey, ORDINAL_SHORT_LABELS } from "../../constants/labels"
import type { TimeMode } from "../../stores/view"
import { ERROR_STATUSES } from "../../utils/projector"
import { POINT_KINDS } from "../../utils/timeline"
import { elapsedUntil, formatDuration } from "../../utils/time"
import type { RecordRow } from "../../utils/view"
import { Status } from "../StatusPills"
import { formatClock, shortId } from "./format"
import { KindBadge } from "./KindBadge"
import { positionOf, type Ordinals } from "./ordinals"
import { COLUMN_VISIBILITY, ROW_GRID, ROW_HEIGHT } from "./tableLayout"

const MAX_INDENT_LEVELS = 8

export interface RowContext {
  ordinals: Ordinals
  /** Recorded time at the position (replay) or now (live); null disables open-interval estimates. */
  clock: string | null
  timeMode: TimeMode
  locale: string
  agentNames: ReadonlyMap<string, string>
}

interface RecordRowViewProps {
  row: RecordRow
  domId: string
  top: number
  selected: boolean
  context: RowContext
  onSelect: (recordId: string) => void
  onToggle: (recordId: string) => void
}

/** One stable business object per row, however many chunks it received. */
export function RecordRowView({
  row,
  domId,
  top,
  selected,
  context,
  onSelect,
  onToggle,
}: RecordRowViewProps) {
  const { t } = useTranslation("admin-trajectories")
  const { record } = row
  const id = record.record_id
  const failed = ERROR_STATUSES.has(record.status ?? "")
  const point = POINT_KINDS.has(record.kind)
  const estimate =
    !point && record.duration_ms === null && record.end_seq === null
      ? elapsedUntil(record.started_at, context.clock)
      : null
  const duration = formatDuration(record.duration_ms ?? estimate, context.locale)
  const position = positionOf(record, context.ordinals)
    .map((part) => t(labelKey(ORDINAL_SHORT_LABELS, part.scope, "position.otherShort"), { n: part.value }))
    .join(" ")
  const agent = record.agent_id ? (context.agentNames.get(record.agent_id) ?? shortId(record.agent_id)) : ""
  return (
    <div
      role="row"
      id={domId}
      aria-level={row.depth + 1}
      aria-expanded={row.childCount ? !row.collapsed : undefined}
      aria-selected={selected}
      data-record-id={id}
      data-kind={record.kind}
      data-testid="trajectory-record-row"
      onClick={() => onSelect(id)}
      className={cn(
        ROW_GRID,
        "border-hairsoft absolute inset-x-0 cursor-pointer items-center gap-x-2 overflow-hidden border-b px-2 text-xs",
        selected ? "bg-a100" : "hover:bg-hairsoft",
        row.context && "opacity-60",
      )}
      style={{ height: ROW_HEIGHT, top: 0, transform: `translateY(${top}px)` }}
    >
      <div
        role="gridcell"
        className="flex min-w-0 items-center gap-1.5 overflow-hidden"
        style={{ paddingInlineStart: `${Math.min(row.depth, MAX_INDENT_LEVELS) * 0.75}rem` }}
      >
        {row.childCount ? (
          <button
            type="button"
            tabIndex={-1}
            aria-label={t(row.collapsed ? "table.expand" : "table.collapse", { count: row.childCount })}
            onClick={(event) => {
              event.stopPropagation()
              onToggle(id)
            }}
            className="text-n500 hover:text-ink flex size-4 flex-none items-center justify-center"
          >
            <ChevronRight
              size={12}
              aria-hidden
              className={cn(
                "transition-transform rtl:rotate-180",
                !row.collapsed && "rotate-90 rtl:rotate-90",
              )}
            />
          </button>
        ) : (
          <span className="size-4 flex-none" />
        )}
        <KindBadge kind={record.kind} className="w-16 overflow-hidden @min-[36rem]/records:w-20" />
        <span className="text-ink min-w-8 shrink truncate font-medium" title={record.title}>
          {record.title}
        </span>
        {record.preview && (
          <span className="text-n500 hidden min-w-0 flex-1 truncate @min-[36rem]/records:inline">
            {record.preview}
          </span>
        )}
        {record.result_preview && (
          <span
            className={cn(
              "hidden min-w-0 truncate @min-[44rem]/records:inline",
              failed ? "text-dangerink" : "text-n600",
            )}
          >
            → {record.result_preview}
          </span>
        )}
      </div>
      <div role="gridcell" className="min-w-0 overflow-hidden whitespace-nowrap">
        <Status scope="record" value={record.status} reason={record.status_reason} />
      </div>
      <div
        role="gridcell"
        className={cn("text-n600 text-2xs truncate font-mono", COLUMN_VISIBILITY.position)}
      >
        {position}
      </div>
      <div role="gridcell" className={cn("text-n600 text-2xs truncate", COLUMN_VISIBILITY.agent)}>
        {agent}
      </div>
      <div role="gridcell" className={cn("text-n600 text-2xs truncate font-mono", COLUMN_VISIBILITY.started)}>
        {formatClock(record.started_at, context.timeMode, context.locale) ?? t("common.dash")}
      </div>
      <div
        role="gridcell"
        className={cn("text-n700 text-2xs truncate text-end font-mono", COLUMN_VISIBILITY.duration)}
      >
        {point || duration === null
          ? t("common.dash")
          : estimate !== null
            ? t("time.estimate", { value: duration })
            : duration}
      </div>
    </div>
  )
}
