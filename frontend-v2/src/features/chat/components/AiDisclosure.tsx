import { useTranslation } from "react-i18next"
import { cn } from "@/shared/lib/cn"
import { legalPath } from "@/shared/legal/links"

/** Separate from the hover-only meta toolbar, including during streaming. */
export function AiGeneratedLabel({ className, visible = true }: { className?: string; visible?: boolean }) {
  const { t } = useTranslation("chat")
  if (!visible) return null
  return <span className={cn("text-n700 inline-flex text-sm font-normal", className)}>{t("aigc.label")}</span>
}

/** A visual preview overlay, not a modification to the underlying asset. */
export function AiWatermark({ className }: { className?: string }) {
  const { t } = useTranslation("chat")
  return (
    <span
      className={cn("ai-media-watermark pointer-events-none absolute end-3 bottom-3 select-none", className)}
    >
      {t("aigc.label")}
    </span>
  )
}

export function AiDisclosure() {
  const { t, i18n } = useTranslation("chat")
  return (
    <div className="text-n700 mt-1 flex flex-wrap items-center justify-center gap-x-2 text-sm leading-5">
      <span>{t("aigc.notice")}</span>
      <a
        href={legalPath("ai", i18n.language)}
        target="_blank"
        rel="noopener noreferrer"
        className="hover:text-ink decoration-n400 min-h-8 content-center underline underline-offset-4 transition-colors"
      >
        {t("aigc.serviceTitle")}
      </a>
    </div>
  )
}
