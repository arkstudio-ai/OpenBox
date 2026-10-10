import { useTranslation } from "react-i18next"
import type { CallControls, CallView } from "../lib/view"
import { HangUpButton } from "./HangUpButton"
import { VoiceOrb } from "./VoiceOrb"

export interface VoiceCallPillProps {
  view: CallView
  controls: CallControls
}

/** The collapsed window: anywhere on it opens the card again, except hang-up. */
export function VoiceCallPill({ view, controls }: VoiceCallPillProps) {
  const { t } = useTranslation("voice")
  const status = t(view.statusKey)
  const dialling = view.status === "requesting_mic" || view.status === "connecting"
  return (
    <div className="bg-card border-hair shadow-pop flex h-11 items-center gap-1 rounded-full border p-1.5">
      <button
        type="button"
        onClick={controls.onExpand}
        aria-label={t("controls.expand")}
        title={t("controls.expand")}
        className="hover:bg-hairsoft flex h-full min-w-0 items-center gap-2 rounded-full ps-1 pe-2.5"
      >
        <VoiceOrb status={view.status} phase={view.phase} muted={view.muted} size="sm" />
        {view.clock && <span className="text-ink flex-none text-sm tabular-nums">{view.clock}</span>}
        {view.clock && (
          <span aria-hidden className="text-n500 flex-none text-sm">
            ·
          </span>
        )}
        <span className="text-ink max-w-40 truncate text-sm">{status}</span>
      </button>
      {/* The button is named for what it does; phase changes are announced here. */}
      <span role="status" aria-live="polite" className="sr-only">
        {status}
      </span>
      <HangUpButton
        onClick={controls.onHangUp}
        tone={dialling ? "cancel" : "hangUp"}
        size="icon"
        disabled={view.status === "ending"}
      />
    </div>
  )
}
