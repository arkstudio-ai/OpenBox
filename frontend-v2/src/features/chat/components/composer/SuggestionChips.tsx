import { useTranslation } from "react-i18next"
import type { NextStepSuggestion } from "@/shared/types/api"

interface Props {
  items: NextStepSuggestion[]
  loading?: boolean
  onSelect: (item: NextStepSuggestion) => void
}

export function SuggestionChips({ items, loading = false, onSelect }: Props) {
  const { t } = useTranslation("chat")
  if (!loading && items.length === 0) return null
  return (
    <div role={loading ? "status" : "group"} aria-label={t(loading ? "suggestions.loading" : "suggestions.title")}
      aria-busy={loading || undefined} className="mb-2 grid grid-flow-col auto-cols-fr gap-1.5">
      {loading ? <>
        <span className="sr-only">{t("suggestions.loading")}</span>
        {[0, 1, 2].map((index) => (
          <span key={index} aria-hidden="true"
            className="suggestion-placeholder border-hair bg-card relative h-7 min-w-0 overflow-hidden rounded-xl border" />
        ))}
      </> : items.slice(0, 3).map((item) => (
        <button
          key={item.label}
          type="button"
          title={t(item.mode === "draft" ? "suggestions.editHint" : "suggestions.sendHint", { prompt: item.prompt })}
          aria-label={t(item.mode === "draft" ? "suggestions.edit" : "suggestions.send", { label: item.label })}
          onClick={() => onSelect(item)}
          className="border-hair bg-card text-n700 hover:border-n400 hover:text-ink active:bg-hairsoft focus-visible:border-n400 focus-visible:ring-n400/50 min-h-11 min-w-0 rounded-xl border px-2.5 py-1.5 text-center text-sm text-balance leading-snug transition-colors outline-none focus-visible:ring-2 sm:px-3"
        >
          <span className="whitespace-normal [overflow-wrap:anywhere]">{item.label}</span>
        </button>
      ))}
    </div>
  )
}
