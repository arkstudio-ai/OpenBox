import { useState } from "react"
import { useTranslation } from "react-i18next"
import { useTrajectoryView } from "../../../stores/view"
import { formatClock } from "../../session/format"
import { AvailabilityNote } from "../AvailabilityNote"
import { ContentActions } from "../ContentActions"
import { JsonTree } from "../JsonTree"
import { useInspector } from "../context"
import { NS, type PanelProps } from "../types"
import { hasSanitizedRaw, isFinalize } from "./eventNotes"

const PAGE = 200

/** Every committed event behind this record up to the shown position, including each streamed chunk. */
export function EventsPanel({ record }: PanelProps) {
  const { t, i18n } = useTranslation(NS)
  const { throughSeq } = useInspector()
  const mode = useTrajectoryView((s) => s.timeMode)
  const [limit, setLimit] = useState(PAGE)
  const events = record.events
  if (!events) return <AvailabilityNote state="not_recorded" />
  const finalized = events.some(isFinalize)
  return (
    <div className="flex flex-col gap-2" data-testid="trajectory-events">
      <div className="flex items-center gap-2">
        <p className="text-n600 min-w-0 flex-1 text-xs">
          {t("events.count", { count: events.length, seq: throughSeq })}
        </p>
        <ContentActions value={events} name={`${record.record_id}-events`} format="jsonl" />
      </div>
      {hasSanitizedRaw(events) && (
        <p className="bg-hairsoft text-n700 rounded-lg px-3 py-2 text-xs">{t("events.sanitizedRawNote")}</p>
      )}
      {finalized && (
        <p className="bg-hairsoft text-n700 rounded-lg px-3 py-2 text-xs">{t("events.finalizeNote")}</p>
      )}
      <ol className="flex flex-col">
        {events.slice(0, limit).map((event) => {
          const chunk = typeof event.data?.chunk_index === "number" ? event.data.chunk_index : null
          return (
            <li
              key={event.event_id}
              className="border-hair border-b last:border-b-0"
              data-event-type={event.type}
            >
              <details>
                <summary className="hover:bg-hairsoft text-2xs flex cursor-pointer flex-wrap items-center gap-x-3 px-1 py-1 font-mono">
                  <span className="text-n500 w-16">{t("events.seq", { seq: event.seq })}</span>
                  <span className="text-ink min-w-0 flex-1 truncate">{event.type}</span>
                  {isFinalize(event) && <span className="text-a700 font-sans">{t("events.finalize")}</span>}
                  {chunk !== null && <span className="text-n500">{t("raw.chunk", { index: chunk })}</span>}
                  <span className="text-n500">{formatClock(event.occurred_at, mode, i18n.language)}</span>
                </summary>
                <div className="flex flex-col gap-2 px-1 pb-2">
                  <JsonTree value={event} openDepth={1} />
                </div>
              </details>
            </li>
          )
        })}
      </ol>
      {events.length > limit && (
        <button
          type="button"
          className="text-a700 w-fit text-xs hover:underline"
          onClick={() => setLimit((value) => value + PAGE)}
        >
          {t("common.showMore", { count: events.length - limit })}
        </button>
      )}
    </div>
  )
}
