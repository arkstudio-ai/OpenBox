import { useTranslation } from "react-i18next"
import type { FieldState } from "../../utils/availability"

export type NoteState = Exclude<FieldState["state"], "available" | "payload">

const KEYS: Readonly<Record<NoteState, string>> = {
  pending: "availability.pending",
  not_recorded: "availability.notRecorded",
  not_applicable: "availability.notApplicable",
  empty: "availability.empty",
  absent: "availability.absent",
  deleted: "availability.deleted",
  unsupported: "availability.unsupported",
  corrupt: "availability.corrupt",
}

interface AvailabilityNoteProps {
  state: NoteState
  reason?: string | null
}

/** Says why there is no value — each case in its own words, never as 0 or a blank. */
export function AvailabilityNote({ state, reason }: AvailabilityNoteProps) {
  const { t } = useTranslation("admin-trajectories")
  const tone = state === "corrupt" || state === "deleted" ? "text-dangerink" : "text-n500"
  return (
    <span className={`${tone} text-xs italic`} data-availability={state}>
      {t(KEYS[state])}
      {reason ? t("availability.reason", { reason }) : null}
    </span>
  )
}
