import { useTranslation } from "react-i18next"
import { StatusPill } from "@/shared/ui/StatusPill"
import {
  labelKey,
  RECORD_STATUS_LABELS,
  RECORD_STATUS_TONES,
  RECORDING_STATUS_LABELS,
  RECORDING_STATUS_TONES,
  RUN_STATUS_LABELS,
  RUN_STATUS_TONES,
} from "../constants/labels"

export type StatusScope = "record" | "run" | "recording"

interface Props {
  scope: StatusScope
  value: string | null | undefined
  reason?: string | null
}

const TABLES = {
  record: { labels: RECORD_STATUS_LABELS, tones: RECORD_STATUS_TONES, other: "status.other" },
  run: { labels: RUN_STATUS_LABELS, tones: RUN_STATUS_TONES, other: "runStatus.other" },
  recording: {
    labels: RECORDING_STATUS_LABELS,
    tones: RECORDING_STATUS_TONES,
    other: "recordingStatus.other",
  },
} as const

/** A lifecycle status in words. Record, run and recording states use separate vocabularies. */
export function Status({ scope, value, reason }: Props) {
  const { t } = useTranslation("admin-trajectories")
  const table = TABLES[scope]
  if (!value) return <StatusPill tone="muted">{t("common.notRecorded")}</StatusPill>
  return (
    <StatusPill tone={table.tones[value] ?? "muted"} title={reason ?? undefined}>
      {t(labelKey(table.labels, value, table.other), { value })}
    </StatusPill>
  )
}
