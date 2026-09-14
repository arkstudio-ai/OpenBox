import { Check, Copy } from "lucide-react"
import { useTranslation } from "react-i18next"
import { useCopy } from "@/shared/hooks/useCopy"
import { cn } from "@/shared/lib/cn"

interface Props {
  text: string
  /** What is copied, for screen readers: "Copy value", "Copy path"… */
  label: string
  className?: string
}

export function CopyButton({ text, label, className }: Props) {
  const { t } = useTranslation("admin-trajectories")
  const { copied, copy } = useCopy()
  return (
    <button
      type="button"
      onClick={(event) => {
        event.preventDefault()
        event.stopPropagation()
        copy(text)
      }}
      aria-label={label}
      title={copied ? t("common.copied") : label}
      className={cn(
        "text-n500 hover:text-ink hover:bg-hairsoft inline-flex size-6 flex-none items-center justify-center rounded",
        className,
      )}
    >
      {copied ? <Check size={12} aria-hidden /> : <Copy size={12} aria-hidden />}
    </button>
  )
}
