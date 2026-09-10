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
      aria-busy={loading || undefined} className="mb-2 grid grid-flow-col auto-cols-fr gap-2 py-1">
      {loading ? <>
        <span className="sr-only">{t("suggestions.loading")}</span>
        {[0, 1, 2].map((index) => (
          <span key={index} aria-hidden="true"
            className="suggestion-placeholder border-hair bg-hairsoft relative h-13 min-w-0 overflow-hidden rounded-lg border" />
        ))}
      </> : items.slice(0, 3).map((item) => (
        <button
          key={item.label}
          type="button"
          title={t(item.mode === "draft" ? "suggestions.editHint" : "suggestions.sendHint", { prompt: item.prompt })}
          aria-label={t(item.mode === "draft" ? "suggestions.edit" : "suggestions.send", { label: item.label })}
          onClick={() => onSelect(item)}
          className="border-hair text-n600 hover:bg-hairsoft hover:text-ink focus-visible:ring-n400 min-h-13 min-w-0 rounded-lg border bg-transparent px-2 py-2 text-center text-sm text-balance leading-snug transition-colors outline-none focus-visible:ring-2 sm:px-3"
        >
          <span className="whitespace-normal [overflow-wrap:anywhere]">{item.label}</span>
        </button>
      ))}
    </div>
  )
}
