import { useTranslation } from "react-i18next"
import type { MessagePart } from "@/shared/types/api"
import { TeamProgressCard } from "@/features/agent-team"
import { ConversationDivider } from "@/features/chat/components/StepDivider"
import { usePanelStore } from "@/features/workbench"
import {
  AgentCreationNotice,
  type AutomaticAgentCreation,
} from "@/features/agent-team/components/AgentCreationNotice"

/** The route composes team content into the chat's ordinary per-turn slot. */
export function TeamTurnTools({ parts, sessionId }: { parts: MessagePart[]; sessionId: string }) {
  const { t } = useTranslation("teams")
  const completed = parts.filter(
    (part) => part.type === "tool" && part.status === "completed" && !part.error && !part.metadata?.error,
  )
  const runIds = new Set<string>()
  const changes: { key: string; kind: "joined" | "retired"; name: string }[] = []
  const creations: AutomaticAgentCreation[] = []
  for (const part of completed) {
    if (part.type !== "tool") continue
    const runId = part.metadata?.team_run_id
    if (part.tool === "team_propose" && part.input?.mode !== "amend" && typeof runId === "string")
      runIds.add(runId)
    const automatic = part.metadata?.agent_autoapproved
    if (Array.isArray(automatic))
      for (const item of automatic.slice(0, 4)) {
        if (
          item &&
          typeof item.definition_id === "string" &&
          typeof item.version_id === "string" &&
          typeof item.revision === "number" &&
          typeof item.name === "string"
        )
          creations.push(item)
      }
    const entries = part.metadata?.team_member_changes
    if (!Array.isArray(entries)) continue
    for (const entry of entries.slice(0, 32)) {
      if (
        entry &&
        (entry.kind === "joined" || entry.kind === "retired") &&
        typeof entry.name === "string" &&
        typeof entry.id === "string"
      ) {
        changes.push({ key: `${part.id}:${entry.id}:${entry.kind}`, kind: entry.kind, name: entry.name })
      }
    }
  }
  return (
    <>
      {creations.map((creation) => (
        <AgentCreationNotice key={creation.definition_id} creation={creation} />
      ))}
      {[...runIds].map((runId) => (
        <TeamProgressCard
          key={runId}
          runId={runId}
          sessionId={sessionId}
          onDetails={() => usePanelStore.getState().openKind("team")}
          onChanges={() =>
            usePanelStore
              .getState()
              .openKind("review", { reviewFile: null, reviewTeamRun: { id: runId, sessionId } })
          }
        />
      ))}
      {changes.map((change) => (
        <ConversationDivider
          key={change.key}
          label={t(change.kind === "joined" ? "memberJoined" : "memberRetired", { name: change.name })}
        />
      ))}
    </>
  )
}
