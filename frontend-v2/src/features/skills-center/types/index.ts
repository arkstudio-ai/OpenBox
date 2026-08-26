// Skill centre domain types. Field names mirror the backend payloads
// (backend/api/metadata.py, backend/skill/catalog.py) — snake_case is kept
// rather than remapped so a field is searchable across both sides.

/** An MCP server's transport. `stdio` runs a local process; `remote` is HTTP. */
export type McpTransport = "stdio" | "remote"

export interface McpConfig {
  type: McpTransport
  command?: string
  args?: string[]
  env?: Record<string, string>
  url?: string
  headers?: Record<string, string>
  timeout?: number
}

/** An MCP server as the container reports it. */
export interface McpServer {
  name: string
  type: string
  status: "connected" | "disconnected" | "error"
  tools: { name: string; description?: string }[]
  resources: unknown[]
  prompts: unknown[]
  error: string | null
  command?: string | null
  args?: string[] | null
  url?: string | null
}

/** An installed skill. `icon`/`requires_mcp` come from SKILL.md frontmatter. */
export interface InstalledSkill {
  name: string
  description?: string
  icon?: string
  requires_mcp?: string[]
  homepage?: string
  /** "container" for user installs, "builtin" for image-baked, "global"/"project" for host. */
  source?: string
  install_dir?: string
  files?: string[]
}

export interface CatalogEnvField {
  key: string
  label: string
  secret?: boolean
}

interface CatalogBase {
  id: string
  name: string
  title: string
  icon: string
  description: string
  publisher?: string
  homepage?: string
  tags?: string[]
  installed: boolean
}

export interface CatalogMcp extends CatalogBase {
  kind: "mcp"
  config: McpConfig
  required_env?: CatalogEnvField[]
}

export interface CatalogSkill extends CatalogBase {
  kind: "skill"
  requires_mcp: string[]
  /** Dependencies not yet installed — resolved server-side so both tabs agree. */
  missing_mcp: string[]
  install: { url?: string; name?: string; content?: string }
}

export interface Catalog {
  skills: CatalogSkill[]
  mcp: CatalogMcp[]
}

export interface CatalogInstallResult {
  ok: boolean
  installed: {
    kind: "skill" | "mcp"
    id: string
    name: string
    status: string
    error?: string | null
  }[]
}

/** Which half of the centre is showing. */
export type CenterTab = "mine" | "store"

/** Which kind of thing the current filter is limited to. */
export type KindFilter = "all" | "skill" | "mcp"
