import { MicOff } from "lucide-react"
import { useTranslation } from "react-i18next"
import { cn } from "@/shared/lib/cn"

interface CallStateProps {
  inCall: boolean
  muted: boolean
}

export function CompanionCallBadge({ inCall, muted }: CallStateProps) {
  if (!inCall) return null
  return (
    <span
      aria-hidden
      className={cn(
        "border-card absolute right-1 bottom-1 flex size-4 items-center justify-center rounded-full border-2",
        muted ? "bg-card text-n700" : "bg-s600",
      )}
    >
      {muted && <MicOff size={10} />}
    </span>
  )
}

export function CompanionEntryLabel({ inCall, muted, name }: CallStateProps & { name: string }) {
  const { t } = useTranslation(["companion", "voice"])
  const label = inCall ? (muted ? t("state.mutedShort", { ns: "voice" }) : t("call.active")) : name
  return (
    <span
      role="status"
      aria-live="polite"
      title={label}
      className="bg-card/90 text-n700 text-2xs mx-auto block w-fit max-w-full truncate rounded-full px-2 py-0.5 text-center shadow-sm"
    >
      {label}
    </span>
  )
}
