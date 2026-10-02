import type { WikiCandidate, WikiPage, WikiSummary } from "../wiki-api"

export interface WikiCitation {
  sourceId: string
  revision: number
  quote: string
  quotes: string[]
}
export function citationsOf(
  page: Pick<WikiPage | WikiCandidate, "body_available" | "paragraphs">,
): WikiCitation[] {
  if (!page.body_available) return []
  const result: WikiCitation[] = []
  for (const paragraph of page.paragraphs) {
    if (!Array.isArray(paragraph.citations)) continue
    for (const raw of paragraph.citations) {
      if (raw && typeof raw.source_id === "string" && typeof raw.quote === "string") {
        const citation = {
          sourceId: raw.source_id,
          revision: Number(raw.revision ?? raw.source_revision ?? 1),
          quote: raw.quote,
          quotes: [raw.quote],
        }
        const existing = result.find(
          (item) => item.sourceId === citation.sourceId && item.revision === citation.revision,
        )
        if (!existing) result.push(citation)
        else if (!existing.quotes.includes(citation.quote)) existing.quotes.push(citation.quote)
      }
    }
  }
  return result
}

export function relatedPages(page: WikiPage, pages: WikiSummary[]) {
  if (!page.body_available) return []
  const sourceIds = new Set(citationsOf(page).map((source) => source.sourceId))
  return pages.filter(
    (other) =>
      other.id !== page.id && other.body_available && other.source_ids.some((id) => sourceIds.has(id)),
  )
}

export function pageConnections(pages: WikiSummary[]) {
  const edges: { from: WikiSummary; to: WikiSummary; sources: number }[] = []
  pages.forEach((page, index) => {
    if (!page.body_available) return
    pages.slice(index + 1).forEach((other) => {
      if (!other.body_available) return
      const sources = page.source_ids.filter((id) => other.source_ids.includes(id)).length
      if (sources) edges.push({ from: page, to: other, sources })
    })
  })
  return edges
}

export function wikiExport(page: WikiPage) {
  if (!page.body_available || !page.body) return ""
  return (
    page.body +
    "\n\n---\n\n" +
    citationsOf(page)
      .map(
        (cite) =>
          "[source:" +
          cite.sourceId +
          "@" +
          cite.revision +
          "]\n\n" +
          cite.quotes.map((quote) => "> " + quote.replaceAll("\n", "\n> ")).join("\n\n"),
      )
      .join("\n\n") +
    "\n"
  )
}
