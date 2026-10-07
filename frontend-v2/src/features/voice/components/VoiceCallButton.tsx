// The assistant's top-bar entry (docs/VOICE_CALL_WEB.md §2), beside "我的任务"
// and in its style. During a call it brings the window back instead of dialling again.
import { Phone } from "lucide-react"
import { useTranslation } from "react-i18next"
import { cn } from "@/shared/lib/cn"
import { useVoiceEnabled } from "../api/voice"
import { useVoiceCall } from "../hooks/useVoiceCall"
import { isLive } from "../lib/reducer"

export function VoiceCallButton() {
  const { t } = useTranslation("voice")
  const enabled = useVoiceEnabled()
  const { status, start, setExpanded } = useVoiceCall()
  const inCall = isLive(status)
  if (!enabled && !inCall) return null
  return (
    <button
      type="button"
      // `start` runs inside the click: the call's audio may only start from a user gesture.
      onClick={inCall ? () => setExpanded(true) : start}
      aria-label={inCall ? t("button.inCall") : t("button.startLabel")}
      className={cn(
        "flex h-8 flex-none items-center gap-1.5 rounded-full border px-3 text-sm",
        inCall ? "border-s300 bg-s100 text-s800" : "border-hair text-n800 hover:bg-hairsoft",
      )}
    >
      <Phone size={15} strokeWidth={2.2} aria-hidden />
      <span className="hidden sm:inline">{inCall ? t("button.inCall") : t("button.start")}</span>
    </button>
  )
}
