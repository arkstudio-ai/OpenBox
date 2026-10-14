import { Mic, MicOff } from "lucide-react"
import { useTranslation } from "react-i18next"
import { cn } from "@/shared/lib/cn"

interface Props {
  muted: boolean
  onClick: () => void
  size?: "icon" | "full"
  compact?: boolean
}

/** The same microphone control in the open window and its collapsed entry. */
export function MuteButton({ muted, onClick, size = "full", compact = false }: Props) {
  const { t } = useTranslation("voice")
  const label = t(muted ? "controls.unmute" : "controls.mute")
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={label}
      aria-pressed={muted}
      title={t(muted ? "controls.unmuteHint" : "controls.muteHint")}
      className={cn(
        "border-hair text-n800 hover:bg-hairsoft flex shrink-0 items-center justify-center gap-1.5 rounded-full border text-sm transition-colors",
        size === "icon" ? (compact ? "size-8" : "size-11") : compact ? "h-8 px-3" : "min-h-11 px-3",
        muted ? "bg-n200" : "bg-card",
      )}
    >
      {muted ? <MicOff size={17} aria-hidden /> : <Mic size={17} aria-hidden />}
      {size === "full" && <span>{label}</span>}
    </button>
  )
}
