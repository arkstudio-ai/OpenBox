import { useTranslation } from "react-i18next"
import { AvailabilityNote } from "../AvailabilityNote"
import { InstantValue } from "../InstantValue"
import { JsonTree } from "../JsonTree"
import { NS, type PanelProps } from "../types"

/**
 * Callbacks as observed: submission, each progress report and the result. A
 * remote system that only reported its end shows no invented progress.
 */
export function JobProgressPanel({ record }: PanelProps) {
  const { t } = useTranslation(NS)
  const events = (record.events ?? []).filter(
    (event) => event.type.startsWith("job.") || event.type === "operation.late_result",
  )
  if (!record.events) return <AvailabilityNote state="not_recorded" />
  const progress = events.filter((event) => event.type === "job.progress").length
  return (
    <div className="flex flex-col gap-3" data-testid="trajectory-job-progress">
      {progress === 0 && <p className="text-n600 text-xs">{t("job.noProgress")}</p>}
      <ol className="flex flex-col gap-2">
        {events.map((event) => (
          <li key={event.event_id} className="border-hair rounded-lg border p-2 text-xs">
            <p className="text-n600 text-2xs mb-1 flex flex-wrap items-center gap-x-3 font-mono">
              <span>{t("events.seq", { seq: event.seq })}</span>
              <span className="text-ink">{event.type}</span>
              <InstantValue iso={event.occurred_at} />
            </p>
            <JsonTree value={event.data} openDepth={1} />
          </li>
        ))}
      </ol>
    </div>
  )
}
