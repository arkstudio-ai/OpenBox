import { Pencil } from "lucide-react"
import { useTranslation } from "react-i18next"
import type { NextStepSuggestion } from "@/shared/types/api"

interface Props {
  items: NextStepSuggestion[]
  onSelect: (item: NextStepSuggestion) => void
}

export function SuggestionChips({ items, onSelect }: Props) {
  const { t } = useTranslation("chat")
  if (items.length === 0) return null
  return (
    <div role="group" aria-label={t("suggestions.title")} className="mb-2 flex gap-2 overflow-x-auto px-5 py-1">
      {items.slice(0, 3).map((item) => (
        <button
          key={item.label}
          type="button"
          title={t(item.mode === "draft" ? "suggestions.editHint" : "suggestions.sendHint", { prompt: item.prompt })}
          aria-label={t(item.mode === "draft" ? "suggestions.edit" : "suggestions.send", { label: item.label })}
          onClick={() => onSelect(item)}
          className="border-hair text-n600 hover:bg-hairsoft hover:text-ink focus-visible:ring-n400 flex h-8 max-w-64 flex-none items-center gap-1.5 rounded-lg border bg-transparent px-3 text-xs transition-colors outline-none focus-visible:ring-2"
        >
          {item.mode === "draft" && <Pencil className="size-3 flex-none" aria-hidden="true" />}
          <span className="truncate">{item.label}</span>
        </button>
      ))}
    </div>
  )
}
