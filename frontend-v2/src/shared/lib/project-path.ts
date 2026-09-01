/** Decode Git's historical C-quoted UTF-8 path without touching normal names. */
function decodeGitQuotedPath(path: string): string {
  if (path.length < 3 || path[0] !== '"' || path.at(-1) !== '"') return path
  const inner = path.slice(1, -1)
  // Requiring an escape avoids stripping quotes from a legitimate filename
  // whose first and last characters happen to be literal quote marks.
  if (!inner.includes("\\")) return path

  const bytes: number[] = []
  const escaped: Record<string, number> = {
    a: 7,
    b: 8,
    t: 9,
    n: 10,
    v: 11,
    f: 12,
    r: 13,
    '"': 34,
    "\\": 92,
  }
  const encoder = new TextEncoder()
  for (let index = 0; index < inner.length; index += 1) {
    const char = inner[index]
    if (char !== "\\") {
      const codePoint = inner.codePointAt(index)
      if (codePoint === undefined) return path
      const literal = String.fromCodePoint(codePoint)
      bytes.push(...encoder.encode(literal))
      index += literal.length - 1
      continue
    }

    const next = inner[index + 1]
    if (!next) return path
    if (/[0-7]/.test(next)) {
      const octal = inner.slice(index + 1).match(/^[0-7]{1,3}/)?.[0]
      if (!octal) return path
      bytes.push(Number.parseInt(octal, 8))
      index += octal.length
      continue
    }
    const value = escaped[next]
    if (value === undefined) return path
    bytes.push(value)
    index += 1
  }

  try {
    const decoded = new TextDecoder("utf-8", { fatal: true }).decode(Uint8Array.from(bytes))
    return decoded.includes("\0") || decoded.includes("\n") ? path : decoded
  } catch {
    return path
  }
}

/** Product-facing notation for paths persisted by old/new workspace layouts. */
export function projectScopedDisplayPath(path: string): string {
  const decoded = decodeGitQuotedPath(path)
  const namespaced = decoded.match(/^\/workspace\/openbox\/users\/[^/]+\/projects\/[^/]+(?:\/(.*))?$/)
  if (namespaced) return namespaced[1] || "."
  const uploaded = decoded.match(
    /^\/workspace\/openbox\/users\/[^/]+\/\.openbox\/uploads\/[^/]+(?:\/(.*))?$/,
  )
  if (uploaded) return uploaded[1] ? `.openbox/uploads/${uploaded[1]}` : ".openbox/uploads"
  const legacy = decoded.match(/^\/workspace\/[^/]+(?:\/(.*))?$/)
  if (legacy) return legacy[1] || "."
  return decoded
}

export function projectScopedDisplayText(text: string): string {
  const physicalPath =
    /\/workspace\/openbox\/users\/[^/\s;,"'<>]+\/(?:projects\/[^/\s;,"'<>]+|\.openbox\/uploads\/[^/\s;,"'<>]+)(?:\/[^\n;,"'<>]*)?/g
  return text
    .split("\n")
    .map((line) => {
      const direct = projectScopedDisplayPath(line)
      if (direct !== line) return direct
      return line.replace(physicalPath, (path) => projectScopedDisplayPath(path))
    })
    .join("\n")
}

const TOOL_PATH_PREFIXES = [
  "*** Update File: ",
  "*** Add File: ",
  "*** Delete File: ",
  "Updated ",
  "Added ",
  "Deleted ",
  "Error on ",
]

/** Rewrite only path-bearing tool protocol lines, never arbitrary file/PTY text. */
export function projectScopedToolText(text: string): string {
  return text
    .split("\n")
    .map((line) => {
      const prefix = TOOL_PATH_PREFIXES.find((candidate) => line.startsWith(candidate))
      return prefix ? `${prefix}${projectScopedDisplayPath(line.slice(prefix.length))}` : line
    })
    .join("\n")
}
