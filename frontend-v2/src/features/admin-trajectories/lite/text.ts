// Text helpers of the lite view.

/** Preview text without Markdown decoration: operators read the words, not the syntax. */
export function plainPreview(text: string | null | undefined): string | null {
  if (!text) return null
  return (
    text
      .replace(/```[a-z]*\n?/g, " ")
      .replace(/^\s{0,3}#{1,6}\s+/gm, "")
      .replace(/^\s*>\s?/gm, "")
      .replace(/^\s*[-*+]\s+/gm, "")
      .replace(/\*\*|__|`/g, "")
      .replace(/!?\[([^\]]*)\]\([^)]*\)/g, "$1")
      .replace(/\s+/g, " ")
      .trim() || null
  )
}
