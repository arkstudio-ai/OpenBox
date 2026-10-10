const escapeRegExp = (value: string) => value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")

/** A card preview: Markdown syntax, citation markers and a heading that only
 *  repeats the title removed, whitespace collapsed. Display only — never fed
 *  back as content. A sentence that merely starts with the title is kept. */
export function plainExcerpt(markdown: string, title = ""): string {
  const heading = title.trim()
  const body = heading
    ? markdown.replace(new RegExp("^\\s*#{1,6}\\s+" + escapeRegExp(heading) + "\\s*(?:\\n|$)"), "")
    : markdown
  return body
    .replace(/\[source:[^\]\s]+\]/g, "")
    .replace(/\[\[([^\]|\n]+)\|?([^\]\n]*)\]\]/g, (_match, target: string, alias: string) => alias || target)
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
    .replace(/^\s{0,3}#{1,6}\s+/gm, "")
    .replace(/^\s*(?:[-*+]|\d+\.)\s+/gm, "")
    .replace(/^\s*>\s?/gm, "")
    .replace(/^\s*\|?(?:\s*:?-{3,}:?\s*\|)+.*$/gm, "")
    .replace(/\|/g, " ")
    .replace(/[*_`~]/g, "")
    .replace(/\s+/g, " ")
    .trim()
}

/** Case-insensitive containment, the same rule the search box applies. */
export function matches(text: string | null | undefined, query: string): boolean {
  return !query || (text ?? "").toLocaleLowerCase().includes(query.toLocaleLowerCase())
}

/** Split text around each case-insensitive occurrence of the query, so the
 *  matched parts can be highlighted without touching the rest. */
export function splitMatches(text: string, query: string): { text: string; hit: boolean }[] {
  const lower = text.toLocaleLowerCase()
  const needle = query.toLocaleLowerCase()
  // Lower-casing can change a string's length (e.g. "İ"); offsets would drift.
  if (!needle || lower.length !== text.length) return [{ text, hit: false }]
  const parts: { text: string; hit: boolean }[] = []
  let start = 0
  for (let index = lower.indexOf(needle); index >= 0; index = lower.indexOf(needle, start)) {
    if (index > start) parts.push({ text: text.slice(start, index), hit: false })
    parts.push({ text: text.slice(index, index + needle.length), hit: true })
    start = index + needle.length
  }
  if (start < text.length) parts.push({ text: text.slice(start), hit: false })
  return parts
}
