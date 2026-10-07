import { PhoneOff } from "lucide-react"
import { useTranslation } from "react-i18next"
import { cn } from "@/shared/lib/cn"

export interface HangUpButtonProps {
  onClick: () => void
  /** Before the call connects the button only cancels, so it is grey rather than red. */
  tone: "cancel" | "hangUp"
  /** The pill has room for the icon alone. */
  size: "icon" | "full"
  disabled?: boolean
}

/** One click hangs up, no confirmation — as on a phone. */
export function HangUpButton({ onClick, tone, size, disabled = false }: HangUpButtonProps) {
  const { t } = useTranslation("voice")
  const label = tone === "cancel" ? t("controls.cancel") : t("controls.hangUp")
  const iconOnly = size === "icon"
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      aria-label={iconOnly ? label : undefined}
      title={iconOnly ? label : undefined}
      className={cn(
        "flex flex-none items-center justify-center gap-1.5 rounded-full text-sm transition-colors disabled:opacity-50",
        iconOnly ? "size-8" : "h-8 px-3",
        tone === "cancel" ? "bg-n200 text-n800 hover:bg-n300" : "bg-danger text-bg hover:bg-dangerink",
      )}
    >
      <PhoneOff size={15} strokeWidth={2.2} aria-hidden />
      {!iconOnly && <span>{label}</span>}
    </button>
  )
}
