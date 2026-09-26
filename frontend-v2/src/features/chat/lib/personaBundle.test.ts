// Composing the persona card's answer: edits ride in the draft, and only a
// 确认 with edits becomes the structured answer the backend parses.
import { describe, expect, it } from "vitest"
import type { QuestionDraftAnswer } from "@/shared/types/api"
import {
  PERSONA_CONFIRM,
  PERSONA_LATER,
  isEditedPersonaAnswer,
  personaAnswer,
  personaEdits,
  readPersonaBundle,
  withPersonaEdit,
} from "./personaBundle"

const items = [
  { memory_id: "m1", type: "IDENTITY", label: "店是谁", summary: "南宁·泽岚鲜果" },
  { memory_id: "m2", type: "VOICE", label: "表达风格", summary: "亲切、直接" },
]
const blank: QuestionDraftAnswer = { selected: [], custom: "", use_custom: false }

describe("reading a persona bundle", () => {
  it("accepts the tool's detail and drops rows without a memory id", () => {
    expect(readPersonaBundle({ kind: "store_persona_bundle", items: [...items, { type: "x" }, "junk"] })).toEqual([
      { memoryId: "m1", type: "IDENTITY", label: "店是谁", summary: "南宁·泽岚鲜果" },
      { memoryId: "m2", type: "VOICE", label: "表达风格", summary: "亲切、直接" },
    ])
  })

  it("ignores every other kind of question detail", () => {
    expect(readPersonaBundle({ kind: "desktop_takeover" })).toBeNull()
    expect(readPersonaBundle({ kind: "store_persona_bundle" })).toBeNull()
    expect(readPersonaBundle(null)).toBeNull()
  })
})

describe("editing and answering", () => {
  const [first, second] = readPersonaBundle({ kind: "store_persona_bundle", items })!

  it("keeps edits in the draft without switching it to a custom answer", () => {
    const draft = withPersonaEdit(blank, first, "南宁·泽岚鲜果，人均 25")
    expect(draft.use_custom).toBe(false)
    expect(personaEdits(draft)).toEqual({ m1: "南宁·泽岚鲜果，人均 25" })
  })

  it("forgets an edit put back to the original", () => {
    const edited = withPersonaEdit(blank, first, "changed")
    const restored = withPersonaEdit(edited, first, first.summary)
    expect(restored.custom).toBe("")
    expect(personaAnswer({ ...restored, selected: [PERSONA_CONFIRM] })).toEqual([PERSONA_CONFIRM])
  })

  it("folds edits into 确认 as the structured answer, and only then", () => {
    const draft = withPersonaEdit(withPersonaEdit(blank, first, "A"), second, "B")
    expect(personaAnswer({ ...draft, selected: [PERSONA_CONFIRM] })).toEqual([
      JSON.stringify({ confirm: true, items: { m1: "A", m2: "B" } }),
    ])
    expect(personaAnswer({ ...draft, selected: [PERSONA_LATER] })).toEqual([PERSONA_LATER])
    expect(personaAnswer({ ...draft, selected: [] })).toEqual([])
    expect(personaAnswer({ ...blank, selected: [PERSONA_CONFIRM] })).toEqual([PERSONA_CONFIRM])
  })

  it("survives a custom field that is not its JSON", () => {
    expect(personaEdits({ ...blank, custom: "free text" })).toEqual({})
    expect(personaEdits({ ...blank, custom: "{not json" })).toEqual({})
    expect(personaEdits({ ...blank, custom: JSON.stringify({ items: { m1: 3 } }) })).toEqual({})
  })

  it("recognises a filed 确认-with-edits answer and nothing else", () => {
    expect(isEditedPersonaAnswer(JSON.stringify({ confirm: true, items: { m1: "A" } }))).toBe(true)
    expect(isEditedPersonaAnswer(JSON.stringify({ confirm: false, items: {} }))).toBe(false)
    expect(isEditedPersonaAnswer(PERSONA_CONFIRM)).toBe(false)
    expect(isEditedPersonaAnswer("{oops")).toBe(false)
  })
})
