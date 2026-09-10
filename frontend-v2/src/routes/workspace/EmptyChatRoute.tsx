import { useState } from "react"
import { useNavigate, useSearchParams } from "react-router"
import { paths } from "@/shared/router/paths"
import { Composer, EmptyState, useChatAgents, useStartChat } from "@/features/chat"
import type { ChatAgent } from "@/features/chat/api/agents"
import { useResourceMention } from "@/features/resources"
import { resolveNewChatProject, useProjectsQuery, useWorkspaceUi } from "@/features/workspace"

const EMPTY_AGENTS: ChatAgent[] = []

/** The "/app" index — the workspace home: greeting + composer. First send
 *  creates the session. */
export default function EmptyChatRoute() {
  const [params] = useSearchParams()
  const selectedProject = useWorkspaceUi((s) => s.selectedProject)
  const projects = useProjectsQuery()
  // The URL names the project when a row opened this page; the logo and a
  // bare /app fall back to the sidebar's current project.
  const { projectId, projectName } = resolveNewChatProject({
    requested: params.get("project"),
    selected: selectedProject,
    projects: projects.data ?? [],
  })
  const navigate = useNavigate()
  const start = useStartChat((sessionId) => navigate(paths.chat(sessionId)))

  // The mode is picked before the session exists, and carried into its
  // creation — starting a conversation in plan mode is exactly when someone
  // wants plan mode, and making them send once in build first would defeat it.
  const { data: agents } = useChatAgents()
  const [agent, setAgent] = useState("build")
  // No session yet, so the menu opens on whichever project this first message
  // will be filed under.
  const resourceScope = useResourceMention(null, projectId)

  return (
    <div className="flex h-full min-h-0 flex-col">
      <EmptyState projectName={projectName} onPick={(text) => void start(text, { projectId, agent })} />
      <Composer
        busy={false}
        autoFocus
        onSubmit={(text, opts) => start(text, { ...opts, projectId, agent })}
        agents={agents ?? EMPTY_AGENTS}
        sessionAgent={agent}
        onPickAgent={setAgent}
        resourceScope={resourceScope}
      />
    </div>
  )
}
