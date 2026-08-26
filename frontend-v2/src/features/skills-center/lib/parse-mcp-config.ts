// Parsing pasted MCP configuration.
//
// People arrive holding a snippet from a README, and those snippets are written
// in whichever shape the tool that documented them uses. Rejecting all but one
// would mean asking someone to retype a config they already have, so every
// shape below maps onto the single {name, config} the backend takes.
import type { McpConfig } from "@/features/skills-center/types"

export interface ParsedMcpEntry {
  name: string
  config: McpConfig
}

export interface ParseResult {
  entries: ParsedMcpEntry[]
  error?: string
}

/** `url` alone is enough to know a server is remote, whatever it calls itself. */
function readTransport(raw: Record<string, unknown>): "stdio" | "remote" {
  const declared = String(raw.type ?? raw.transport ?? "").toLowerCase()
  if (declared === "stdio") return "stdio"
  if (["remote", "http", "sse", "streamable-http", "streamablehttp"].includes(declared)) {
    return "remote"
  }
  return raw.url ? "remote" : "stdio"
}

function readStringMap(value: unknown): Record<string, string> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {}
  const out: Record<string, string> = {}
  for (const [k, v] of Object.entries(value as Record<string, unknown>)) {
    if (v !== null && v !== undefined) out[k] = String(v)
  }
  return out
}

function readArgs(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  return value.map((v) => String(v))
}

function readOne(name: string, raw: unknown): ParsedMcpEntry | null {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null
  const obj = raw as Record<string, unknown>
  const type = readTransport(obj)

  if (type === "remote") {
    const url = typeof obj.url === "string" ? obj.url.trim() : ""
    if (!url) return null
    return {
      name,
      config: {
        type: "remote",
        url,
        headers: readStringMap(obj.headers),
        timeout: Number(obj.timeout) || 60,
      },
    }
  }

  const command = typeof obj.command === "string" ? obj.command.trim() : ""
  if (!command) return null
  return {
    name,
    config: {
      type: "stdio",
      command,
      args: readArgs(obj.args),
      env: readStringMap(obj.env),
      timeout: Number(obj.timeout) || 60,
    },
  }
}

/**
 * Read one or more servers out of pasted JSON.
 *
 * Accepted shapes:
 *   {"mcpServers": {"name": {...}}}   Claude Desktop / Cursor / Windsurf
 *   {"servers": {"name": {...}}}      VS Code
 *   {"name": {...}}                   a bare map of servers
 *   {"command": "npx", ...}           a single server, named by `fallbackName`
 */
export function parseMcpConfig(text: string, fallbackName = ""): ParseResult {
  const trimmed = text.trim()
  if (!trimmed) return { entries: [], error: "empty" }

  let data: unknown
  try {
    data = JSON.parse(trimmed)
  } catch {
    return { entries: [], error: "invalidJson" }
  }
  if (!data || typeof data !== "object" || Array.isArray(data)) {
    return { entries: [], error: "invalidShape" }
  }

  const root = data as Record<string, unknown>
  const container = (root.mcpServers ?? root.servers ?? root.mcp_servers) as unknown

  if (container && typeof container === "object" && !Array.isArray(container)) {
    const entries: ParsedMcpEntry[] = []
    for (const [name, value] of Object.entries(container as Record<string, unknown>)) {
      const parsed = readOne(name, value)
      if (parsed) entries.push(parsed)
    }
    return entries.length ? { entries } : { entries: [], error: "noServers" }
  }

  // A single server object, recognised by carrying a transport field itself.
  if (root.command || root.url) {
    const name = (typeof root.name === "string" && root.name.trim()) || fallbackName.trim()
    if (!name) return { entries: [], error: "needName" }
    const parsed = readOne(name, root)
    return parsed ? { entries: [parsed] } : { entries: [], error: "invalidShape" }
  }

  // A bare map whose values are server objects.
  const entries: ParsedMcpEntry[] = []
  for (const [name, value] of Object.entries(root)) {
    const parsed = readOne(name, value)
    if (parsed) entries.push(parsed)
  }
  return entries.length ? { entries } : { entries: [], error: "noServers" }
}
