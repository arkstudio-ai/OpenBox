import { useEffect, useId, useLayoutEffect, useMemo, useRef, type KeyboardEvent } from "react"
import { useTranslation } from "react-i18next"
import { useVirtualizer } from "@tanstack/react-virtual"
import { cn } from "@/shared/lib/cn"
import type { RecordRow } from "../../utils/view"
import { RecordRowView, type RowContext } from "./RecordRowView"
import { COLUMN_VISIBILITY, ROW_GRID, ROW_HEIGHT } from "./tableLayout"

interface RecordTableProps {
  rows: readonly RecordRow[]
  selectedId: string | null
  onSelect: (recordId: string) => void
  onToggle: (recordId: string) => void
  context: RowContext
}

const PAGE_ROWS = 10

/**
 * The records at the shown position as a virtualised tree grid, keyed by
 * record id. It opens at the tail and follows new records only while the
 * reader is at the bottom; scrolled up, the first visible record stays put
 * when rows are inserted. Arrow keys move the selection, Left/Right fold.
 */
export function RecordTable({ rows, selectedId, onSelect, onToggle, context }: RecordTableProps) {
  const { t } = useTranslation("admin-trajectories")
  const baseId = useId()
  const scrollRef = useRef<HTMLDivElement>(null)
  const following = useRef(true)
  const anchor = useRef<{ id: string; offset: number } | null>(null)
  // eslint-disable-next-line react-hooks/incompatible-library -- tanstack virtual is the project's chosen virtualizer
  const virtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => ROW_HEIGHT,
    getItemKey: (index) => rows[index].record.record_id,
    overscan: 12,
    initialRect: { width: 960, height: 480 },
  })
  const selectedIndex = useMemo(
    () => (selectedId ? rows.findIndex((row) => row.record.record_id === selectedId) : -1),
    [rows, selectedId],
  )

  useLayoutEffect(() => {
    const element = scrollRef.current
    if (!element) return
    if (following.current) {
      element.scrollTop = element.scrollHeight
      return
    }
    const kept = anchor.current
    const index = kept ? rows.findIndex((row) => row.record.record_id === kept.id) : -1
    if (kept && index >= 0) element.scrollTop = index * ROW_HEIGHT + kept.offset
  }, [rows])

  useEffect(() => {
    if (selectedIndex >= 0) virtualizer.scrollToIndex(selectedIndex, { align: "auto" })
  }, [selectedIndex, virtualizer])

  const onScroll = () => {
    const element = scrollRef.current
    if (!element) return
    following.current = element.scrollHeight - element.scrollTop - element.clientHeight < ROW_HEIGHT * 2
    const first = Math.floor(element.scrollTop / ROW_HEIGHT)
    anchor.current = rows[first]
      ? { id: rows[first].record.record_id, offset: element.scrollTop - first * ROW_HEIGHT }
      : null
  }

  const move = (index: number) => {
    if (!rows.length) return
    const clamped = Math.max(0, Math.min(rows.length - 1, index))
    onSelect(rows[clamped].record.record_id)
    virtualizer.scrollToIndex(clamped, { align: "auto" })
  }

  const fold = (key: string) => {
    const row = rows[selectedIndex]
    if (!row) return move(0)
    const expandable = row.childCount > 0
    if (key === "ArrowRight")
      return expandable && row.collapsed
        ? onToggle(row.record.record_id)
        : expandable
          ? move(selectedIndex + 1)
          : undefined
    if (expandable && !row.collapsed) return onToggle(row.record.record_id)
    for (let index = selectedIndex - 1; index >= 0; index -= 1)
      if (rows[index].depth < row.depth) return move(index)
  }

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const steps: Record<string, number> = {
      ArrowDown: selectedIndex + 1,
      ArrowUp: Math.max(0, selectedIndex - 1),
      PageDown: selectedIndex + PAGE_ROWS,
      PageUp: selectedIndex - PAGE_ROWS,
      Home: 0,
      End: rows.length - 1,
    }
    if (event.key in steps) move(steps[event.key])
    else if (event.key === "ArrowRight" || event.key === "ArrowLeft") fold(event.key)
    else if ((event.key === "Enter" || event.key === " ") && rows[selectedIndex]?.childCount)
      onToggle(rows[selectedIndex].record.record_id)
    else return
    event.preventDefault()
  }

  return (
    <div
      role="treegrid"
      aria-label={t("table.label")}
      aria-rowcount={rows.length}
      aria-activedescendant={selectedIndex >= 0 ? `${baseId}-r${selectedIndex}` : undefined}
      tabIndex={0}
      onKeyDown={onKeyDown}
      className="focus-visible:outline-a700 @container/records flex min-h-0 min-w-0 flex-1 flex-col focus-visible:outline-2 focus-visible:-outline-offset-2"
      data-testid="trajectory-record-list"
    >
      <div
        role="row"
        className={cn(
          ROW_GRID,
          "border-hair text-n500 text-2xs gap-x-2 overflow-hidden border-b px-2 py-1.5 whitespace-nowrap",
        )}
      >
        <span role="columnheader" className="truncate">
          {t("table.record")}
        </span>
        <span role="columnheader" className="truncate">
          {t("table.status")}
        </span>
        <span role="columnheader" className={cn("truncate", COLUMN_VISIBILITY.position)}>
          {t("table.position")}
        </span>
        <span role="columnheader" className={cn("truncate", COLUMN_VISIBILITY.agent)}>
          {t("table.agent")}
        </span>
        <span role="columnheader" className={cn("truncate", COLUMN_VISIBILITY.started)}>
          {t("table.started")}
        </span>
        <span role="columnheader" className={cn("truncate text-end", COLUMN_VISIBILITY.duration)}>
          {t("table.duration")}
        </span>
      </div>
      {!rows.length ? (
        <p className="text-n500 p-4 text-xs">{t("table.empty")}</p>
      ) : (
        <div ref={scrollRef} onScroll={onScroll} className="min-h-0 flex-1 overflow-auto">
          <div role="rowgroup" className="relative w-full" style={{ height: virtualizer.getTotalSize() }}>
            {virtualizer.getVirtualItems().map((item) => (
              <RecordRowView
                key={item.key}
                row={rows[item.index]}
                domId={`${baseId}-r${item.index}`}
                top={item.start}
                selected={item.index === selectedIndex}
                context={context}
                onSelect={onSelect}
                onToggle={onToggle}
              />
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
