import { useMemo, useState } from "react"
import { useTranslation } from "react-i18next"
import { X } from "lucide-react"
import { Composer, type ComposerProps } from "@/features/chat/components/Composer"
import { useConfigQuery } from "@/features/chat/api/config"
import type { MentionItem, MentionSection } from "@/features/chat/hooks/useMentionMenu"
import { TeamPicker, useDefinitions, type AgentSpec, type TeamSpec } from "@/features/agent-team"
import type { TeamRequest } from "@/shared/types/api"

/** The route composes library selection into the shared chat input. */
export function TeamComposer({ initialTemplate, ...props }: ComposerProps & { initialTemplate?: string | null }) {
  const { t } = useTranslation("teams")
  const config = useConfigQuery()
  const enabled = Boolean(config.data?.team_ui_enabled && config.data.team_admission_enabled)
  const agentsQuery = useDefinitions<AgentSpec>("agent", "", "active", enabled)
  const teamsQuery = useDefinitions<TeamSpec>("team", "", "active", enabled)
  const agents = useMemo(() => agentsQuery.data?.pages.flatMap((page) => [...page.items, ...(page.builtin ?? [])]) ?? [], [agentsQuery.data])
  const templates = useMemo(() => teamsQuery.data?.pages.flatMap((page) => page.items) ?? [], [teamsQuery.data])
  const [request, setRequest] = useState<TeamRequest>({ template_id: initialTemplate })
  const [seenKey, setSeenKey] = useState(props.sessionKey)
  if (seenKey !== props.sessionKey) { setSeenKey(props.sessionKey); setRequest({ template_id: initialTemplate }) }
  const sections = useMemo<MentionSection[]>(() => enabled ? [
    { kind: "agent", loading: agentsQuery.isLoading, items: agents.map((entry) => ({ id: entry.id, kind: "agent", label: entry.name, description: entry.version.spec.description, insert: "" })) },
    { kind: "team", loading: teamsQuery.isLoading, items: templates.map((entry) => ({ id: entry.id, kind: "team", label: entry.name, description: entry.version.spec.description, insert: "" })) },
  ] : [], [enabled, agents, templates, agentsQuery.isLoading, teamsQuery.isLoading])
  const pickMention = (item: MentionItem) => {
    props.onPickAgent?.("team")
    setRequest((value) => item.kind === "team" ? { ...value, template_id: item.id } : { ...value, requested_agent_ids: [...new Set([...(value.requested_agent_ids ?? []), item.id])] })
  }
  return <Composer {...props}
    teamMentions={sections}
    onPickTeamMention={pickMention}
    teamPicker={enabled ? <TeamPicker templates={templates} value={request} onChange={setRequest} disabled={props.busy} /> : undefined}
    teamSelection={props.sessionAgent === "team" && !!request.requested_agent_ids?.length && <div className="flex flex-wrap gap-1.5 px-4 pt-3">
      {request.requested_agent_ids.map((id) => <span key={id} className="bg-hairsoft flex items-center gap-1 rounded-full px-2.5 py-1 text-xs">
        {agents.find((entry) => entry.id === id)?.name ?? id}
        <button type="button" aria-label={t("removeSelection")} onClick={() => setRequest({ ...request, requested_agent_ids: request.requested_agent_ids?.filter((value) => value !== id) })}><X className="size-3" /></button>
      </span>)}
    </div>}
    onSubmit={(text, opts) => props.onSubmit(text, { ...opts, teamRequest: props.sessionAgent === "team" ? request : undefined })}
  />
}
