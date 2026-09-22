import type { CatalogSkill } from "../api/teams"
import type { AgentSpec, TeamPolicy } from "../types"
import { CORE_TOOLS } from "./defaults"

export interface CapabilityRequirements {
  tools: string[]
  servers: string[]
  categories: string[]
}

export function skillRequirements(
  spec: Pick<AgentSpec, "skill_mode" | "skill_refs">,
  skills: CatalogSkill[],
  core = CORE_TOOLS,
  tiers: Record<string, string> = {},
): CapabilityRequirements {
  const selected =
    spec.skill_mode === "selected"
      ? skills.filter((skill) =>
          spec.skill_refs.some(
            (ref) => ref.name === skill.name && (!ref.source || ref.source === skill.source),
          ),
        )
      : []
  const loaders = selected.length || spec.skill_mode === "all_accessible" ? ["skill", "skill_search"] : []
  const tools = [...new Set([...core, ...loaders, ...selected.flatMap((skill) => skill.allowed_tools ?? [])])]
  const servers = [...new Set(selected.flatMap((skill) => skill.requires_mcp ?? []))]
  return {
    tools,
    servers,
    categories: [
      ...new Set([
        "T0",
        ...tools.flatMap((tool) => (tiers[tool] ? [tiers[tool]] : [])),
        ...(servers.length ? ["MCP"] : []),
      ]),
    ],
  }
}

export function includeRequirements(spec: AgentSpec, required: CapabilityRequirements): AgentSpec {
  const refs = new Map(spec.mcp_refs.map((ref) => [ref.server, ref]))
  for (const server of required.servers) refs.set(server, { server, tools: ["*"] })
  return {
    ...spec,
    tool_allowlist: [...new Set([...spec.tool_allowlist, ...required.tools])],
    mcp_refs: [...refs.values()],
    execution_policy: {
      ...spec.execution_policy,
      tool_categories: spec.execution_policy.tool_categories.length
        ? [...new Set([...spec.execution_policy.tool_categories, ...required.categories])]
        : [],
    },
  }
}

export function includeTeamRequirements(policy: TeamPolicy, required: CapabilityRequirements): TeamPolicy {
  const refs = new Map(policy.mcp_refs.map((ref) => [ref.server, ref]))
  for (const server of required.servers) refs.set(server, { server, tools: ["*"] })
  return {
    ...policy,
    delegable_tools: [...new Set([...policy.delegable_tools, ...required.tools])],
    mcp_refs: [...refs.values()],
  }
}
