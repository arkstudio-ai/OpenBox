import { useTranslation } from "react-i18next"
import { useTrajectoryView, type TimeMode } from "../../stores/view"
import { formatInstant } from "../../utils/time"
import { AvailabilityNote } from "./AvailabilityNote"
import { NS } from "./types"

interface InstantValueProps {
  iso: string | null | undefined
}

const MODE_LABELS: Readonly<Record<TimeMode, string>> = {
  local: "time.mode.local",
  utc: "time.mode.utc",
  unix: "time.mode.unix",
}

/** A recorded instant with milliseconds; clicking cycles local time, UTC and Unix seconds. */
export function InstantValue({ iso }: InstantValueProps) {
  const { t, i18n } = useTranslation(NS)
  const mode = useTrajectoryView((s) => s.timeMode)
  const cycle = useTrajectoryView((s) => s.cycleTimeMode)
  const text = formatInstant(iso, mode, i18n.language)
  if (text === null) return <AvailabilityNote state="not_recorded" />
  // Inline text rather than a flex row: in a narrow column the zone label wraps after the time, not beside it.
  return (
    <button
      type="button"
      onClick={cycle}
      title={t("time.cycle")}
      className="hover:bg-hairsoft -mx-1 rounded px-1 text-start"
    >
      <span className="font-mono">{text}</span>
      <span className="text-n500 text-2xs ms-1.5">{t(MODE_LABELS[mode])}</span>
    </button>
  )
}
