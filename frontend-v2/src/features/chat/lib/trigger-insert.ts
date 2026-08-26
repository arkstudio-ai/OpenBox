// Typing the "@" that opens the mention menu, for the entries that reach it
// without a keystroke (the "+" menu, and the slash menu's resource row).
//
// Pure so the rule that matters — never glue the trigger onto the previous
// word, or resolveTrigger would read "note@" as part of that word and refuse
// to fire — can be unit-tested without a textarea.

export interface TriggerInsertion {
  text: string
  caret: number
}

export function insertMentionTrigger(text: string, caret: number): TriggerInsertion {
  const at = Math.min(Math.max(caret, 0), text.length)
  const insert = at > 0 && !/\s/.test(text[at - 1] ?? "") ? " @" : "@"
  return { text: text.slice(0, at) + insert + text.slice(at), caret: at + insert.length }
}
