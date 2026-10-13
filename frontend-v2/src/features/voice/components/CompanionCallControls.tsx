import { Mic, MicOff, Phone, PhoneOff } from "lucide-react"
import { useTranslation } from "react-i18next"
import { useVoiceEnabled } from "../api/voice"
import { useCallElapsed, useVoiceCall } from "../hooks/useVoiceCall"
import { isLive } from "../lib/reducer"
import { callView } from "../lib/view"
import { CostTag } from "./CostTag"
import { VoiceCallEnded } from "./VoiceCallEnded"
import { VoiceOrb } from "./VoiceOrb"

export function CompanionCallControls() {
  const { t } = useTranslation(["companion", "voice"])
  const call = useVoiceCall()
  const enabled = useVoiceEnabled()
  const elapsed = useCallElapsed(call.status, call.startedAt, call.stoppedAt)
  const view = callView(call, elapsed)
  if (call.status === "ended" && call.ended)
    return (
      <div className="companion-call-ended px-4 py-3">
        <VoiceCallEnded ended={call.ended} onRedial={enabled ? call.start : null} onClose={call.dismiss} />
      </div>
    )
  if (!isLive(call.status))
    return (
      <div className="flex items-center justify-between gap-3 px-5 py-3">
        <p className="text-n700 text-xs leading-5">{t(enabled ? "call.ready" : "call.unavailable")}</p>
        <button
          type="button"
          onClick={call.start}
          disabled={!enabled}
          className="bg-ink text-bg flex min-h-11 shrink-0 items-center gap-2 rounded-full px-4 text-sm font-medium hover:opacity-90 disabled:opacity-40"
        >
          <Phone size={16} aria-hidden />
          {t("call.start")}
        </button>
      </div>
    )
  return (
    <div className="bg-surface/60 space-y-2 px-4 py-3">
      <div className="flex items-center gap-2">
        <VoiceOrb status={view.status} phase={view.phase} muted={view.muted} size="sm" />
        <span role="status" aria-live="polite" className="min-w-0 flex-1 text-sm">
          {t(view.statusKey, { ns: "voice" })}
        </span>
        {view.clock && <span className="text-n700 text-xs tabular-nums">{view.clock}</span>}
        {call.status === "connected" && (
          <button
            type="button"
            onClick={call.toggleMute}
            aria-label={t(call.muted ? "controls.unmute" : "controls.mute", { ns: "voice" })}
            aria-pressed={call.muted}
            className="border-hair hover:bg-hairsoft flex size-11 shrink-0 items-center justify-center rounded-full border"
          >
            {call.muted ? <MicOff size={17} aria-hidden /> : <Mic size={17} aria-hidden />}
          </button>
        )}
        <button
          type="button"
          onClick={call.hangUp}
          disabled={call.status === "ending"}
          aria-label={t("controls.hangUp", { ns: "voice" })}
          className="bg-danger-soft text-danger-ink flex size-11 shrink-0 items-center justify-center rounded-full disabled:opacity-40"
        >
          <PhoneOff size={17} aria-hidden />
        </button>
      </div>
      {view.cost && (
        <div className="flex items-center justify-between gap-2 text-xs">
          <span className="text-n700">{t("call.continues")}</span>
          <CostTag cost={view.cost} />
        </div>
      )}
    </div>
  )
}
