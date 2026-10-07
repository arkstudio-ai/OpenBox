// The floating call window (docs/VOICE_CALL_WEB.md §3). The workspace shell
// mounts it once, so a call outlives route changes; between calls it renders
// nothing. Below dialogs and sheets (z-50) and toasts (z-60) on purpose.
import { useTranslation } from "react-i18next"
import { REDIAL_REASONS } from "../constants/copy"
import { useAutoDismiss, useCallElapsed, useVoiceCall } from "../hooks/useVoiceCall"
import { callView, type CallControls } from "../lib/view"
import { useVoiceStore } from "../store"
import { VoiceCallEnded } from "./VoiceCallEnded"
import { VoiceCallExpanded } from "./VoiceCallExpanded"
import { VoiceCallPill } from "./VoiceCallPill"

export function VoiceCallDock() {
  const idle = useVoiceStore((state) => state.call.status === "idle")
  // A fresh window per connected call, so a redial never shows the last call's clock.
  const startedAt = useVoiceStore((state) => state.call.startedAt)
  return idle ? null : <CallWindow key={startedAt ?? "dialling"} />
}

function CallWindow() {
  const { t } = useTranslation("voice")
  const call = useVoiceCall()
  const elapsed = useCallElapsed(call.status, call.startedAt, call.stoppedAt)
  useAutoDismiss(call.ended, call.dismiss)
  const controls: CallControls = {
    onCollapse: () => call.setExpanded(false),
    onExpand: () => call.setExpanded(true),
    onToggleMute: call.toggleMute,
    onHangUp: call.hangUp,
  }
  let body
  if (call.status === "ended" && call.ended) {
    const redial = REDIAL_REASONS.has(call.ended.reason) ? call.start : null
    body = <VoiceCallEnded ended={call.ended} onRedial={redial} onClose={call.dismiss} />
  } else {
    const view = callView(call, elapsed)
    body = call.expanded ? (
      <VoiceCallExpanded view={view} controls={controls} />
    ) : (
      <VoiceCallPill view={view} controls={controls} />
    )
  }
  return (
    <div role="region" aria-label={t("title")} className="fixed end-4 top-15 z-40">
      {body}
    </div>
  )
}
