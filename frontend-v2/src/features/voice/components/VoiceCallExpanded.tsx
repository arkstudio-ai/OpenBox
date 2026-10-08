import { ChevronUp, Mic, MicOff } from "lucide-react"
import { useTranslation } from "react-i18next"
import { useAssistantNames } from "@/shared/appearance/useAssistantNames"
import { cn } from "@/shared/lib/cn"
import type { CallControls, CallView } from "../lib/view"
import { CostTag } from "./CostTag"
import { HangUpButton } from "./HangUpButton"
import { VoiceOrb } from "./VoiceOrb"

export interface VoiceCallExpandedProps {
  view: CallView
  controls: CallControls
}

/** The full window: orb, title and clock; what is happening; mute, cost, hang up. */
export function VoiceCallExpanded({ view, controls }: VoiceCallExpandedProps) {
  const { t } = useTranslation("voice")
  // The call is with the assistant the person named: the window says so, as the voice does.
  const name = useAssistantNames().title
  const connected = view.status === "connected"
  const dialling = view.status === "requesting_mic" || view.status === "connecting"
  // Hanging up freezes the clock and keeps the cost in view at once; the
  // server's settled figures replace them when `ended` arrives.
  const showCost = (connected || view.status === "ending") && view.cost !== null
  return (
    <div className="bg-card border-hair shadow-pop w-75 rounded-2xl border p-3">
      <div className="flex items-center gap-2.5">
        {/* The orb collapses the window too; keyboard users have the chevron. */}
        <button
          type="button"
          tabIndex={-1}
          aria-hidden
          onClick={controls.onCollapse}
          className="flex flex-none rounded-full"
        >
          <VoiceOrb status={view.status} phase={view.phase} muted={view.muted} size="md" />
        </button>
        <span className="text-ink text-md min-w-0 flex-1 truncate font-medium">{name}</span>
        {view.remainingMinutes !== null && (
          <span className="text-a700 flex-none text-xs">
            {t("duration.remaining", { minutes: view.remainingMinutes })}
          </span>
        )}
        {view.clock && <span className="text-n700 flex-none text-sm tabular-nums">{view.clock}</span>}
        <button
          type="button"
          onClick={controls.onCollapse}
          aria-label={t("controls.collapse")}
          title={t("controls.collapse")}
          className="text-n700 hover:bg-hairsoft flex size-7 flex-none items-center justify-center rounded-full"
        >
          <ChevronUp size={16} strokeWidth={2.2} aria-hidden />
        </button>
      </div>
      <div role="status" aria-live="polite" className="mt-2 flex min-h-6 items-center gap-2">
        {dialling && (
          <span
            aria-hidden
            className="animate-spin-arc border-n300 border-t-a700 inline-block size-3.5 flex-none rounded-full border-2"
          />
        )}
        <span className="text-ink text-base">{t(view.statusKey)}</span>
        {view.noteKey && <span className="text-n600 ms-auto flex-none text-xs">{t(view.noteKey)}</span>}
      </div>
      {view.showHint && <p className="text-n600 mt-0.5 text-xs">{t("hint.start")}</p>}
      <div className="border-hair mt-3 flex items-center gap-2 border-t pt-3">
        {connected && (
          <button
            type="button"
            onClick={controls.onToggleMute}
            className={cn(
              "border-hair text-n800 hover:bg-hairsoft flex h-8 flex-none items-center gap-1.5 rounded-full border px-3 text-sm",
              view.muted && "bg-n200",
            )}
          >
            {view.muted ? (
              <MicOff size={15} strokeWidth={2.2} aria-hidden />
            ) : (
              <Mic size={15} strokeWidth={2.2} aria-hidden />
            )}
            {view.muted ? t("controls.unmute") : t("controls.mute")}
          </button>
        )}
        <span className="min-w-0 flex-1" />
        {showCost && view.cost && <CostTag cost={view.cost} />}
        <HangUpButton
          onClick={controls.onHangUp}
          tone={dialling ? "cancel" : "hangUp"}
          size="full"
          disabled={view.status === "ending"}
        />
      </div>
    </div>
  )
}
