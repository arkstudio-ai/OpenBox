import type { WikiCitation } from "./content"
import type { WikiPage, WikiSummary } from "../wiki-api"

/** Local OKF paths can only resolve to currently authorized pages or sources. */
export function exchangeHref(href: string | undefined, page: Partial<WikiPage>) {
  if (!href || !page.exchange_path || href.startsWith("#") || /^[a-z][a-z\d+.-]*:/i.test(href)) return href
  try {
    const target = new URL(href, "https://wiki.invalid/" + page.exchange_path)
    if (target.origin !== "https://wiki.invalid") return undefined
    const link = page.exchange_links?.[decodeURIComponent(target.pathname.slice(1))]
    if (link?.page_id) return "#wiki-page-" + link.page_id
    if (link?.source_id) return "#wiki-import-source-" + link.source_id
  } catch {
    /* Malformed or unresolved local file paths remain inert. */
  }
  return undefined
}

interface Node {
  type: string
  value?: string
  url?: string
  depth?: number
  children?: Node[]
  data?: { hProperties: Record<string, unknown> }
  position?: { start: { offset?: number }; end: { offset?: number } }
}
interface Options {
  citations: WikiCitation[]
  pages: WikiSummary[]
  projectId?: string | null
}

export function headingId(text: string) {
  return "wiki-" + text.trim().toLocaleLowerCase().replace(/\s+/g, "-")
}
function textOf(node: Node): string {
  return node.value ?? node.children?.map(textOf).join("") ?? ""
}

/** Transform text nodes only. Code, images and existing links remain inert. */
export function remarkWiki(options: Options) {
  return (tree: Node, file?: { value?: unknown }) => {
    const usedHeadings = new Map<string, number>()
    const walk = (node: Node) => {
      if (["link", "image", "code", "inlineCode", "html"].includes(node.type)) return
      if (node.type === "heading") {
        const id = headingId(textOf(node))
        const count = usedHeadings.get(id) ?? 0
        usedHeadings.set(id, count + 1)
        node.data = { hProperties: { id: count ? id + "-" + count : id } }
      }
      if (!node.children) return
      node.children = node.children.flatMap((child) => {
        if (child.type !== "text") {
          walk(child)
          return [child]
        }
        const text = child.value ?? ""
        // Markdown has already unescaped text. If the original node contains
        // escapes/entities, preserve it literally rather than fabricate a link.
        const raw =
          typeof file?.value === "string"
            ? file.value.slice(child.position?.start.offset, child.position?.end.offset)
            : text
        if (raw.includes("\\") || /&#?(?:[a-zA-Z]+|\d+|x[0-9a-fA-F]+);/.test(raw)) return [child]
        const pattern = /\[source:([^\]@\s]+)@(\d+)\]|\[\[([^\]\n]+)\]\]/g
        const parts: Node[] = []
        let start = 0
        let lastUrl = ""
        for (const match of text.matchAll(pattern)) {
          let url = "",
            label = match[0]
          if (match[1]) {
            const index = options.citations.findIndex(
              (c) => c.sourceId === match[1] && c.revision === Number(match[2]),
            )
            if (index >= 0) {
              url = "#wiki-citation-" + index
              label = String(index + 1)
            }
          } else {
            const [target, alias] = match[3].split("|")
            const matches = options.pages.filter(
              (page) =>
                page.body_available &&
                page.project_id === options.projectId &&
                (page.slug === target || page.title === target),
            )
            if (matches.length === 1) {
              url = "#wiki-page-" + matches[0].id
              label = alias || matches[0].title
            }
          }
          if (!url) continue
          if (
            url === lastUrl &&
            url.startsWith("#wiki-citation-") &&
            !text.slice(start, match.index).trim()
          ) {
            start = match.index + match[0].length
            continue
          }
          parts.push(
            { type: "text", value: text.slice(start, match.index) },
            { type: "link", url, children: [{ type: "text", value: label }] },
          )
          start = match.index + match[0].length
          lastUrl = url
        }
        return parts.length ? [...parts, { type: "text", value: text.slice(start) }] : [child]
      })
    }
    walk(tree)
    attachCitations(tree)
  }
}

const isCitation = (node: Node) => node.type === "link" && !!node.url?.startsWith("#wiki-citation-")

/** The paragraph, or last list item or quote paragraph, that a citation can end. */
function lastParagraph(node: Node | undefined): Node | undefined {
  if (node?.type === "paragraph") return node
  if (node && ["list", "listItem", "blockquote"].includes(node.type))
    return lastParagraph(node.children?.at(-1))
  return undefined
}

/**
 * Compiled pages put each citation on its own line after the text it supports.
 * Read as a lone number between paragraphs, so show it at the end of that text.
 */
function attachCitations(parent: Node) {
  if (!parent.children) return
  const kept: Node[] = []
  for (const node of parent.children) {
    attachCitations(node)
    const target = lastParagraph(kept.at(-1))
    const children = node.children ?? []
    const onlyCitations =
      node.type === "paragraph" &&
      children.some(isCitation) &&
      children.every((child) => isCitation(child) || (child.type === "text" && !child.value?.trim()))
    if (target && onlyCitations) {
      target.children = [...(target.children ?? []), ...children.filter(isCitation)]
      continue
    }
    kept.push(node)
  }
  parent.children = kept
}

export function remarkOutline() {
  return (tree: Node) => {
    remarkWiki({ citations: [], pages: [] })(tree)
    tree.children = tree.children?.filter((node) => node.type === "heading" && (node.depth ?? 0) > 1)
  }
}
