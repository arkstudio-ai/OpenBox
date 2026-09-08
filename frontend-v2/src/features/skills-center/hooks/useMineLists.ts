// The "我的" tab's two lists, narrowed by the shared search box.
//
// Every skill the agent can reach is listed, including the ones baked into the
// sandbox image and the ones living on the backend. Hiding those made the list
// an inventory of what is removable rather than of what the agent has, so a
// capability like dev-browser was simply invisible. Removability is a property
// of a row, not a reason to omit it.
import { useMemo } from "react"
import type { InstalledSkill, McpServer } from "@/features/skills-center/types"

function matches(query: string, ...fields: (string | undefined)[]): boolean {
  const q = query.trim().toLowerCase()
  if (!q) return true
  return fields.some((f) => (f ?? "").toLowerCase().includes(q))
}

export function useMineLists(
  skills: InstalledSkill[] | undefined,
  servers: McpServer[],
  query: string,
): { skills: InstalledSkill[]; servers: McpServer[] } {
  const filteredSkills = useMemo(
    () => (skills ?? []).filter((s) => matches(query, s.name, s.description)),
    [skills, query],
  )
  const filteredServers = useMemo(
    () => servers.filter((s) => matches(query, s.name, s.url ?? s.command ?? "")),
    [servers, query],
  )
  return { skills: filteredSkills, servers: filteredServers }
}
