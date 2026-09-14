// Line diff for "what changed" views (system prompt, tool catalog, compaction).
// Plain LCS over lines, bounded: beyond the limit the caller shows both sides
// in full instead of an expensive or misleading partial diff.

export type DiffOp = "same" | "add" | "del"

export interface DiffLine {
  op: DiffOp
  text: string
}

export const DIFF_LINE_LIMIT = 1500

export function lineDiff(before: string, after: string, limit = DIFF_LINE_LIMIT): DiffLine[] | null {
  const a = before.split("\n")
  const b = after.split("\n")
  if (a.length > limit || b.length > limit) return null
  const width = b.length + 1
  const table = new Uint16Array((a.length + 1) * width)
  for (let i = a.length - 1; i >= 0; i -= 1) {
    for (let j = b.length - 1; j >= 0; j -= 1) {
      table[i * width + j] =
        a[i] === b[j]
          ? table[(i + 1) * width + j + 1] + 1
          : Math.max(table[(i + 1) * width + j], table[i * width + j + 1])
    }
  }
  const lines: DiffLine[] = []
  let i = 0
  let j = 0
  while (i < a.length && j < b.length) {
    if (a[i] === b[j]) {
      lines.push({ op: "same", text: a[i] })
      i += 1
      j += 1
    } else if (table[(i + 1) * width + j] >= table[i * width + j + 1]) {
      lines.push({ op: "del", text: a[i] })
      i += 1
    } else {
      lines.push({ op: "add", text: b[j] })
      j += 1
    }
  }
  for (; i < a.length; i += 1) lines.push({ op: "del", text: a[i] })
  for (; j < b.length; j += 1) lines.push({ op: "add", text: b[j] })
  return lines
}

/** Stable text for structured values so key order does not show up as a change. */
export function stableText(value: unknown): string {
  if (typeof value === "string") return value
  if (value === undefined) return ""
  return JSON.stringify(sortKeys(value), null, 2)
}

function sortKeys(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(sortKeys)
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.keys(value as Record<string, unknown>)
        .sort()
        .map((key) => [key, sortKeys((value as Record<string, unknown>)[key])]),
    )
  }
  return value
}
