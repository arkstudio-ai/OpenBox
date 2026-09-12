import { useTranslation } from "react-i18next"
import { formatDuration, formatExactMs } from "../../utils/time"
import { AvailabilityNote } from "./AvailabilityNote"
import { NS } from "./types"

interface DurationValueProps {
  ms: number | null | undefined
  /** An open interval measured against the position's clock, not a recorded duration. */
  estimate?: boolean
}

export function DurationValue({ ms, estimate = false }: DurationValueProps) {
  const { t, i18n } = useTranslation(NS)
  const text = formatDuration(ms, i18n.language)
  if (text === null) return <AvailabilityNote state="not_recorded" />
  const exact = formatExactMs(ms, i18n.language) ?? text
  return (
    <span title={exact} className="font-mono">
      {estimate ? t("time.estimate", { value: text }) : text}
    </span>
  )
}
