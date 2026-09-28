// The store persona card (docs/OPS_CASE_PLAN.md §4.3): five editable memory
// summaries on one `question` whose `detail.kind` is "store_persona_bundle"
// (backend/tool/creator_context.py `propose_bundle`).
//
// Edits ride in the draft's `custom` field as JSON, so autosave carries them
// across reloads and devices like any other draft. `use_custom` stays false:
// the pills (确认 / 稍后) remain the answer, and only at submit are the edits
// folded into the 确认 answer as the structured string the backend parses
// (backend/memory/service.py `parse_bundle_answer`).
import type { QuestionDraftAnswer } from "@/shared/types/api"

/** Option labels the backend files the card with (memory_service.BUNDLE_*). */
export const PERSONA_CONFIRM = "确认"
export const PERSONA_LATER = "稍后"

export interface PersonaBundleItem {
  memoryId: string
  type: string
  label: string
  summary: string
}

function text(record: Record<string, unknown>, key: string): string {
  const value = record[key]
  return typeof value === "string" ? value : ""
}

/** The items of a store_persona_bundle question, or null for anything else. */
export function readPersonaBundle(value: unknown): PersonaBundleItem[] | null {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return null
  const record = value as Record<string, unknown>
  if (record.kind !== "store_persona_bundle" || !Array.isArray(record.items)) return null
  const items = record.items.flatMap((raw): PersonaBundleItem[] => {
    if (raw === null || typeof raw !== "object" || Array.isArray(raw)) return []
    const item = raw as Record<string, unknown>
    const memoryId = text(item, "memory_id")
    if (!memoryId) return []
    return [{ memoryId, type: text(item, "type"), label: text(item, "label"), summary: text(item, "summary") }]
  })
  return items
}

/** memory id → edited summary, as stored in the draft's `custom`. */
export function personaEdits(draft: QuestionDraftAnswer): Record<string, string> {
  if (!draft.custom.startsWith("{")) return {}
  try {
    const data = JSON.parse(draft.custom) as { items?: unknown }
    if (data === null || typeof data !== "object" || data.items === null || typeof data.items !== "object") return {}
    return Object.fromEntries(
      Object.entries(data.items as Record<string, unknown>).filter((e): e is [string, string] => typeof e[1] === "string"),
    )
  } catch {
    return {}
  }
}

/** The draft with one field edited; a field put back to its original leaves
 *  no edit behind, so an untouched card still answers with the plain label. */
export function withPersonaEdit(
  draft: QuestionDraftAnswer,
  item: PersonaBundleItem,
  value: string,
): QuestionDraftAnswer {
  const edits = { ...personaEdits(draft) }
  if (value === item.summary) delete edits[item.memoryId]
  else edits[item.memoryId] = value
  const custom = Object.keys(edits).length > 0 ? JSON.stringify({ items: edits }) : ""
  return { ...draft, custom, use_custom: false }
}

/** What the card submits: 确认 with edits becomes the structured answer;
 *  everything else (plain 确认, 稍后, nothing yet) is the pill itself. */
export function personaAnswer(draft: QuestionDraftAnswer): string[] {
  const edits = personaEdits(draft)
  if (draft.selected.includes(PERSONA_CONFIRM) && Object.keys(edits).length > 0) {
    return [JSON.stringify({ confirm: true, items: edits })]
  }
  return draft.selected
}

/** Whether a filed answer is the structured 确认-with-edits form. */
export function isEditedPersonaAnswer(answer: string): boolean {
  if (!answer.startsWith("{")) return false
  try {
    const data = JSON.parse(answer) as { confirm?: unknown; items?: unknown }
    return data !== null && typeof data === "object" && data.confirm === true
      && data.items !== null && typeof data.items === "object"
  } catch {
    return false
  }
}
