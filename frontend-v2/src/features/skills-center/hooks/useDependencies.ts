// Working out what a skill still needs, and doing something about it.
//
// The same question — which of this skill's declared servers are not usable
// right now — is asked in two places: on an installed row, and straight after
// an install so the gap never reaches the person as a chore.
import { useCallback, useMemo } from "react"
import type { Dependency } from "@/features/skills-center/components/DependencyDialog"
import type { CatalogMcp, InstalledSkill, McpServer } from "@/features/skills-center/types"

export function useDependencyResolver(servers: McpServer[], catalogMcp: CatalogMcp[]) {
  const byName = useMemo(() => new Map(servers.map((s) => [s.name, s])), [servers])
  const catalogByName = useMemo(
    () => new Map(catalogMcp.map((c) => [c.name, c])),
    [catalogMcp],
  )

  /**
   * Dependencies of `skill` that are not usable yet.
   *
   * "Configured but disconnected" and "not there at all" both leave the skill
   * broken, but they need different fixes, so they are distinguished rather
   * than lumped into one "missing".
   */
  return useCallback(
    (skill: Pick<InstalledSkill, "requires_mcp">): Dependency[] => {
      const out: Dependency[] = []
      for (const name of skill.requires_mcp ?? []) {
        const server = byName.get(name)
        if (server?.status === "connected") continue
        out.push({
          name,
          configured: Boolean(server),
          catalog: catalogByName.get(name),
        })
      }
      return out
    },
    [byName, catalogByName],
  )
}
