// The store persona card: what the agent inferred about the shop, one
// editable field per memory, above the 确认 / 稍后 pills QuestionDock draws.
//
// It rides on a `question` whose `detail.kind` is "store_persona_bundle"
// (backend/tool/creator_context.py). The free-text box is not offered for
// this kind — the fields are the free text — so the dock hides it and the
// edits travel in the draft instead (lib/personaBundle.ts).
import { useTranslation } from "react-i18next"
import type { QuestionDraftAnswer, QuestionItem } from "@/shared/types/api"
import { personaEdits, readPersonaBundle, withPersonaEdit } from "../lib/personaBundle"

interface Props {
  item: QuestionItem
  draft: QuestionDraftAnswer
  onChange: (draft: QuestionDraftAnswer) => void
}

export function StorePersonaDetail({ item, draft, onChange }: Props) {
  const { t } = useTranslation("chat")
  const items = readPersonaBundle(item.detail)
  if (!items || items.length === 0) return null
  const edits = personaEdits(draft)

  return (
    <div className="border-hair bg-bg flex flex-col gap-2.5 rounded-lg border p-3">
      <span className="text-n600 text-xs">{t("question.persona.hint")}</span>
      {items.map((entry) => {
        const edited = entry.memoryId in edits
        return (
          <label key={entry.memoryId} className="flex flex-col gap-1">
            <span className="flex items-center gap-2 text-xs">
              <span className="text-ink font-medium">{entry.label || entry.type}</span>
              {edited && <span className="bg-a100 text-a800 rounded-full px-2 py-0.5">{t("question.persona.edited")}</span>}
            </span>
            <textarea
              value={edited ? edits[entry.memoryId] : entry.summary}
              maxLength={2000}
              rows={2}
              onChange={(event) => onChange(withPersonaEdit(draft, entry, event.target.value))}
              className="border-hair bg-card text-ink focus:border-accent w-full resize-y rounded-lg border px-2.5 py-2 text-sm leading-6 outline-none"
            />
          </label>
        )
      })}
    </div>
  )
}
