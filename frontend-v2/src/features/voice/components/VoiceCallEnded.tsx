import { useTranslation } from "react-i18next"
import { formatAmount, formatClock } from "@/shared/lib/format"
import { END_COPY, ERROR_COPY } from "../constants/copy"
import type { CallEnd } from "../lib/types"
import { VoiceOrb } from "./VoiceOrb"

export interface VoiceCallEndedProps {
  ended: CallEnd
  /** Absent when calling straight back would only be refused again. */
  onRedial: (() => void) | null
  onClose: () => void
}

/** What happened, how long it took and what it cost; anything still being worked on goes to the conversation. */
export function VoiceCallEnded({ ended, onRedial, onClose }: VoiceCallEndedProps) {
  const { t } = useTranslation("voice")
  const detail = ended.errorMessage ?? (ended.errorKey ? t(ERROR_COPY[ended.errorKey]) : null)
  const facts = [
    ended.durationSeconds > 0 ? t("ended.duration", { duration: formatClock(ended.durationSeconds) }) : null,
    ended.cost ? t("ended.cost", { yuan: formatAmount(ended.cost.total_yuan, 4) }) : null,
  ].filter((fact) => fact !== null)
  return (
    <div className="bg-card border-hair shadow-pop w-75 rounded-2xl border p-3.5">
      <div className="flex items-center gap-2.5">
        <VoiceOrb status="ended" phase="listening" size="md" />
        <span className="text-ink text-md font-medium">{t("ended.title")}</span>
      </div>
      <div role="status" aria-live="polite" className="mt-2 space-y-1">
        {/* "Call ended" would only repeat the title. */}
        {ended.reason !== "hangup" && (
          <p className="text-ink text-sm leading-relaxed">{t(END_COPY[ended.reason])}</p>
        )}
        {detail && <p className="text-n600 text-xs leading-relaxed">{detail}</p>}
        {facts.length > 0 && <p className="text-n600 text-xs tabular-nums">{facts.join(" · ")}</p>}
        {ended.pendingTurns > 0 && (
          <p className="text-a700 text-xs leading-relaxed">
            {t("ended.pendingHint", { count: ended.pendingTurns })}
          </p>
        )}
      </div>
      <div className="mt-3 flex justify-end gap-2">
        {onRedial && (
          <button
            type="button"
            onClick={onRedial}
            className="bg-ink text-bg h-8 rounded-full px-3.5 text-sm hover:opacity-90"
          >
            {t("actions.redial")}
          </button>
        )}
        <button
          type="button"
          onClick={onClose}
          className="border-hair text-n800 hover:bg-hairsoft h-8 rounded-full border px-3.5 text-sm"
        >
          {t("actions.close")}
        </button>
      </div>
    </div>
  )
}
