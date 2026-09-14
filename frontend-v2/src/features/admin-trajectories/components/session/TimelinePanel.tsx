import { useEffect, useId, useMemo, useRef, useState, type KeyboardEvent } from "react"
import { useTranslation } from "react-i18next"
import { ChevronDown, ZoomIn, ZoomOut } from "lucide-react"
import { cn } from "@/shared/lib/cn"
import { KIND_LABELS, LANE_CLASSES, LANE_LABELS, LANES, labelKey } from "../../constants/labels"
import type { TimelineScale } from "../../stores/view"
import type { Seq } from "../../types/protocol"
import { layoutTimeline } from "../../utils/timeline"
import type { ViewRecord } from "../../utils/view"
import { timelineMarks } from "./timelineMarks"

interface TimelinePanelProps {
  records: readonly ViewRecord[]
  seq: Seq
  clock: string | null
  selectedId: string | null
  onSelect: (recordId: string) => void
  scale: TimelineScale
  onScale: (scale: TimelineScale) => void
}

const ZOOMS = [1, 2, 4, 8, 16] as const
const SCALES: readonly TimelineScale[] = ["sequence", "duration"]
const SCALE_LABELS: Readonly<Record<TimelineScale, string>> = {
  sequence: "timeline.scaleSequence",
  duration: "timeline.scaleDuration",
}
const ICON_BUTTON = "text-n600 hover:bg-hairsoft rounded p-1 disabled:opacity-30"

/**
 * The recording at the shown position as coloured lanes: model requests,
 * tools, interactions, agents and lifecycle. Intervals are bars, instants are
 * dots; nothing after the position is drawn. Arrow keys walk the marks. It can
 * be folded away so the record table gets the height.
 */
export function TimelinePanel({
  records,
  seq,
  clock,
  selectedId,
  onSelect,
  scale,
  onScale,
}: TimelinePanelProps) {
  const { t } = useTranslation("admin-trajectories")
  const bodyId = useId()
  const [zoom, setZoom] = useState(0)
  const [open, setOpen] = useState(true)
  const scroller = useRef<HTMLDivElement>(null)
  const layout = useMemo(
    () => layoutTimeline(records, { scale, headSeq: seq, clock }),
    [clock, records, scale, seq],
  )
  const marks = useMemo(() => timelineMarks(layout.items), [layout.items])
  const byId = useMemo(() => new Map(records.map((record) => [record.record_id, record])), [records])
  const lanes = LANES.filter((lane) => marks.some((mark) => mark.lane === lane))
  const selectedIndex = selectedId ? marks.findIndex((mark) => mark.recordIds.includes(selectedId)) : -1

  useEffect(() => {
    if (selectedIndex < 0 || !open) return
    scroller.current
      ?.querySelector(`[data-mark-index="${selectedIndex}"]`)
      ?.scrollIntoView?.({ block: "nearest", inline: "nearest" })
  }, [open, selectedIndex, zoom])

  const labelOf = (recordIds: readonly string[]) => {
    if (recordIds.length > 1) return t("timeline.merged", { count: recordIds.length })
    const record = byId.get(recordIds[0])
    if (!record) return recordIds[0]
    return t("timeline.markLabel", {
      kind: t(labelKey(KIND_LABELS, record.kind, "kind.other"), { value: record.kind }),
      title: record.title,
    })
  }

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (!marks.length) return
    const moves: Record<string, number> = {
      ArrowRight: selectedIndex + 1,
      ArrowLeft: selectedIndex - 1,
      Home: 0,
      End: marks.length - 1,
    }
    if (event.key in moves) {
      event.preventDefault()
      const index = Math.max(0, Math.min(marks.length - 1, moves[event.key]))
      onSelect(marks[index].recordIds[0])
    } else if (event.key === "+" || event.key === "=") {
      setZoom((value) => Math.min(ZOOMS.length - 1, value + 1))
    } else if (event.key === "-") {
      setZoom((value) => Math.max(0, value - 1))
    }
  }

  return (
    <section
      aria-label={t("timeline.title")}
      className="border-hair bg-card flex flex-col gap-1.5 rounded-xl border px-3 py-2"
      data-testid="trajectory-timeline"
    >
      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          aria-expanded={open}
          aria-controls={bodyId}
          onClick={() => setOpen((value) => !value)}
          title={t(open ? "timeline.hide" : "timeline.show")}
          className="text-ink hover:bg-hairsoft -ms-1 inline-flex items-center gap-1 rounded px-1 text-xs font-medium"
          data-testid="trajectory-timeline-toggle"
        >
          <ChevronDown
            size={13}
            aria-hidden
            className={cn("transition-transform", !open && "-rotate-90 rtl:rotate-90")}
          />
          {t("timeline.title")}
        </button>
        <span className="text-n500 text-2xs font-mono">{t("timeline.upTo", { seq })}</span>
        <span className="flex-1" />
        {open && scale === "duration" && layout.untimed > 0 && (
          <span className="text-n500 text-2xs">{t("timeline.untimed", { count: layout.untimed })}</span>
        )}
        {open && (
          <>
            <div
              role="group"
              aria-label={t("timeline.scale")}
              className="border-hair flex rounded-full border p-0.5"
            >
              {SCALES.map((option) => (
                <button
                  key={option}
                  type="button"
                  aria-pressed={scale === option}
                  onClick={() => onScale(option)}
                  className={cn(
                    "text-2xs rounded-full px-2.5 py-0.5",
                    scale === option ? "bg-ink text-bg" : "text-n600 hover:text-ink",
                  )}
                >
                  {t(SCALE_LABELS[option])}
                </button>
              ))}
            </div>
            <button
              type="button"
              aria-label={t("timeline.zoomOut")}
              title={t("timeline.zoomOut")}
              disabled={zoom === 0}
              onClick={() => setZoom((value) => value - 1)}
              className={ICON_BUTTON}
            >
              <ZoomOut size={14} aria-hidden />
            </button>
            <button
              type="button"
              aria-label={t("timeline.zoomIn")}
              title={t("timeline.zoomIn")}
              disabled={zoom === ZOOMS.length - 1}
              onClick={() => setZoom((value) => value + 1)}
              className={ICON_BUTTON}
            >
              <ZoomIn size={14} aria-hidden />
            </button>
          </>
        )}
      </div>
      {open && !marks.length && (
        <p id={bodyId} className="text-n500 py-2 text-xs">
          {t("timeline.empty")}
        </p>
      )}
      {open && marks.length > 0 && (
        <div id={bodyId} className="flex min-w-0">
          <div className="flex w-20 flex-none flex-col sm:w-24" aria-hidden>
            {lanes.map((lane) => (
              <span key={lane} className="text-n600 text-2xs flex h-4 items-center truncate">
                {t(LANE_LABELS[lane])}
              </span>
            ))}
          </div>
          <div
            ref={scroller}
            role="group"
            tabIndex={0}
            aria-label={t("timeline.keyboardHint")}
            onKeyDown={onKeyDown}
            className="focus-visible:outline-a700 relative min-w-0 flex-1 overflow-x-auto rounded focus-visible:outline-2"
          >
            <div className="relative" style={{ width: `${ZOOMS[zoom] * 100}%` }}>
              {lanes.map((lane) => (
                <div key={lane} className="border-hairsoft relative h-4 border-b">
                  {marks.map((mark, index) =>
                    mark.lane !== lane ? null : (
                      <button
                        key={mark.key}
                        type="button"
                        tabIndex={-1}
                        data-mark-index={index}
                        data-record-ids={mark.recordIds.length === 1 ? mark.recordIds[0] : undefined}
                        aria-label={labelOf(mark.recordIds)}
                        title={labelOf(mark.recordIds)}
                        onClick={() => onSelect(mark.recordIds[0])}
                        className={cn(
                          "absolute top-0.5 h-2.5 rounded-sm",
                          LANE_CLASSES[mark.lane],
                          mark.point && "w-2 rounded-full",
                          mark.open && "opacity-70",
                          mark.tone === "danger" && "ring-danger ring-2",
                          mark.tone === "warn" && "ring-a700 ring-1",
                          index === selectedIndex && "outline-ink outline-2 outline-offset-1",
                        )}
                        style={{
                          insetInlineStart: `${mark.start * 100}%`,
                          width: mark.point ? undefined : `max(2px, ${(mark.end - mark.start) * 100}%)`,
                        }}
                      />
                    ),
                  )}
                </div>
              ))}
              <span aria-hidden className="bg-ink absolute inset-y-0 end-0 w-px" />
            </div>
          </div>
        </div>
      )}
    </section>
  )
}
