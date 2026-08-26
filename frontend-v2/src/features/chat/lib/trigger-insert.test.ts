import { describe, expect, it } from "vitest"
import { insertMentionTrigger } from "./trigger-insert"

describe("insertMentionTrigger", () => {
  it("types a bare @ into an empty composer", () => {
    expect(insertMentionTrigger("", 0)).toEqual({ text: "@", caret: 1 })
  })

  it("separates the trigger from the preceding word", () => {
    // "look@" would not open the menu — resolveTrigger needs a boundary.
    expect(insertMentionTrigger("look", 4)).toEqual({ text: "look @", caret: 6 })
  })

  it("keeps a single space when one is already there", () => {
    expect(insertMentionTrigger("look ", 5)).toEqual({ text: "look @", caret: 6 })
  })

  it("inserts at the caret, not at the end", () => {
    expect(insertMentionTrigger("ab cd", 3)).toEqual({ text: "ab @cd", caret: 4 })
  })

  it("clamps a caret outside the text", () => {
    expect(insertMentionTrigger("ab", 99)).toEqual({ text: "ab @", caret: 4 })
  })
})
