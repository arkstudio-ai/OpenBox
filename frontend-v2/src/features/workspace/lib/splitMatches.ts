/** ``text`` cut at each case-insensitive occurrence of ``words``. */
export function splitMatches(text: string, words: string): { text: string; match: boolean }[] {
  const needle = words.toLowerCase()
  if (!needle) return [{ text, match: false }]
  const lower = text.toLowerCase()
  const pieces: { text: string; match: boolean }[] = []
  let from = 0
  for (let at = lower.indexOf(needle); at >= 0; at = lower.indexOf(needle, from)) {
    if (at > from) pieces.push({ text: text.slice(from, at), match: false })
    pieces.push({ text: text.slice(at, at + needle.length), match: true })
    from = at + needle.length
  }
  if (from < text.length) pieces.push({ text: text.slice(from), match: false })
  return pieces
}
