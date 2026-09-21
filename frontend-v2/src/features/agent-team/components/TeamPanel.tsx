import { useState, type ComponentType } from "react"
import { Link } from "react-router"
import { useTranslation } from "react-i18next"
import { ArrowUpRight, Users } from "lucide-react"
import { paths } from "@/shared/router/paths"
import { cn } from "@/shared/lib/cn"
import { Spinner } from "@/shared/ui/Spinner"
import { useTeamCollection, useTeamRun, useTeamRuns } from "../api/teams"
import type { TeamArtifact, TeamMessage, TeamSnapshot, TeamTask } from "../types"
import { TeamRosterGraph } from "./TeamRosterGraph"
import { TeamControls, TeamRunAttention } from "./TeamProgressCard"
import { SaveTeamConfiguration } from "./SaveTeamConfiguration"
import { terminalTeam } from "../types"
import { TeamUsagePanel } from "./TeamUsagePanel"
import { TeamTaskRow } from "./TeamTaskRow"

type Tab = "members" | "tasks" | "messages" | "artifacts" | "usage"
const TABS: Tab[] = ["members", "tasks", "messages", "artifacts", "usage"]

export interface TeamArtifactPreviewProps {
  artifacts: TeamArtifact[]
  finalIds: string[]
}

function Roster({
  team,
  selected,
  onSelect,
  onMessages,
  onTask,
}: {
  team: TeamSnapshot
  selected: string | null
  onSelect: (id: string) => void
  onMessages: (pair: string) => void
  onTask: (id: string) => void
}) {
  const { t } = useTranslation("teams")
  const member = team.members.find((entry) => entry.id === selected)
  return (
    <>
      <TeamRosterGraph team={team} selected={selected} onSelect={onSelect} onMessages={onMessages} />
      {member ? (
        <div className="border-hair bg-card space-y-3 rounded-xl border p-4">
          <div className="flex items-center justify-between gap-2">
            <strong className="text-sm">{member.name}</strong>
            <span className="text-n600 text-xs">{member.model}</span>
          </div>
          <p className="text-n600 text-sm">{member.responsibility || member.description}</p>
          <p className="text-n600 text-xs break-words">
            {t("tools")}: {member.tool_ids.join(", ")}
          </p>
          {member.skill_refs.length > 0 && (
            <p className="text-n600 text-xs">
              {t("skills")}: {member.skill_refs.map((skill) => skill.name).join(" · ")}
            </p>
          )}
          {team.tasks
            .filter((task) => task.current_attempt && task.current_attempt === member.current_attempt)
            .map((task) => (
              <button
                key={task.id}
                type="button"
                className="text-a700 block text-start text-xs"
                onClick={() => onTask(task.id)}
              >
                {t("currentTask")}: {task.title}
              </button>
            ))}
          {member.error && (
            <p role="alert" className="text-danger text-xs">
              {member.error}
            </p>
          )}
          <Link
            to={member.role === "coordinator" ? paths.chat(team.run.root_session_id) : paths.chat(member.id)}
            className="text-a700 inline-flex items-center gap-1 text-xs"
          >
            {t(member.role === "coordinator" ? "openChat" : "openMember")}
            <ArrowUpRight className="size-3" />
          </Link>
          {member.role === "coordinator" && <TeamControls team={team} />}
          {member.role === "member" && <SaveTeamConfiguration team={team} member={member} />}
        </div>
      ) : (
        <p className="text-n600 p-4 text-center text-xs">{t("selectMember")}</p>
      )}
    </>
  )
}

function TaskList({ id, team, focusTask }: { id: string; team: TeamSnapshot; focusTask: string | null }) {
  const { t } = useTranslation("teams")
  const tasks = useTeamCollection<TeamTask>(id, "tasks")
  const focused = useTeamCollection<TeamTask>(id, "tasks", { task_id: focusTask ?? undefined }, !!focusTask)
  const entries = tasks.data?.pages.flatMap((page) => page.items) ?? []
  const focusEntry = focused.data?.pages[0]?.items[0]
  const visible =
    focusEntry && !entries.some((task) => task.id === focusEntry.id) ? [focusEntry, ...entries] : entries
  return (
    <div className="space-y-2">
      {visible.length === 0 && <p className="text-n600 p-4 text-sm">{t("noTasks")}</p>}
      {visible.map((task) => (
        <TeamTaskRow key={task.id} task={task} team={team} focused={task.id === focusTask} />
      ))}
      {tasks.hasNextPage && (
        <button
          type="button"
          disabled={tasks.isFetchingNextPage}
          onClick={() => void tasks.fetchNextPage()}
          className="text-a700 text-sm"
        >
          {t("loadMore")}
        </button>
      )}
      {(tasks.error || focused.error) && (
        <p role="alert" className="text-danger text-xs">
          {tasks.error?.message || focused.error?.message}
        </p>
      )}
    </div>
  )
}
function Messages({
  team,
  pair,
  setPair,
}: {
  team: TeamSnapshot
  pair: string
  setPair: (pair: string) => void
}) {
  const { t } = useTranslation("teams")
  const [member, setMember] = useState("")
  const messages = useTeamCollection<TeamMessage>(team.id, "messages", {
    member: member || undefined,
    between: pair || undefined,
  })
  return (
    <div className="space-y-3">
      <label className="text-n600 flex items-center gap-2 text-xs">
        {t("filterMember")}
        <select
          value={member}
          onChange={(event) => setMember(event.target.value)}
          className="border-hair bg-card min-w-0 rounded-lg border px-2 py-1.5"
        >
          <option value="">{t("allMembers")}</option>
          {team.members.map((entry) => (
            <option key={entry.id} value={entry.id}>
              {entry.name}
            </option>
          ))}
        </select>
      </label>
      {pair && (
        <button type="button" className="text-a700 text-xs" onClick={() => setPair("")}>
          {t("clearPair")}
        </button>
      )}
      {messages.data?.pages
        .flatMap((page) => page.items)
        .map((message) => (
          <article key={message.id} className="border-hair bg-card rounded-xl border p-3">
            <div className="text-n600 mb-2 flex flex-wrap gap-2 text-xs">
              <span>
                {t("messageFromTo", {
                  from: team.members.find((entry) => entry.id === message.from_member_id)?.name,
                  to: team.members.find((entry) => entry.id === message.to_member_id)?.name,
                })}
              </span>
              <span>{t(`messageKind.${message.kind}`, { defaultValue: message.kind })}</span>
              <span>{t(`state.${message.state}`, { defaultValue: message.state })}</span>
            </div>
            <p className="text-sm break-words whitespace-pre-wrap">{message.body}</p>
          </article>
        ))}
      {messages.data?.pages[0]?.items.length === 0 && (
        <p className="text-n600 py-4 text-sm">{t("noMessages")}</p>
      )}
      {messages.hasNextPage && (
        <button
          type="button"
          onClick={() => void messages.fetchNextPage()}
          disabled={messages.isFetchingNextPage}
          className="text-a700 text-sm"
        >
          {t("loadMore")}
        </button>
      )}
      {messages.error && (
        <p role="alert" className="text-danger text-xs">
          {messages.error.message}
        </p>
      )}
    </div>
  )
}
function Artifacts({
  team,
  ArtifactPreview,
}: {
  team: TeamSnapshot
  ArtifactPreview?: ComponentType<TeamArtifactPreviewProps>
}) {
  const { t } = useTranslation("teams")
  const artifacts = useTeamCollection<TeamArtifact>(team.id, "artifacts")
  const entries = artifacts.data?.pages.flatMap((page) => page.items) ?? []
  return (
    <div className="space-y-3">
      {team.run.final_summary && (
        <div className="bg-card border-hair rounded-xl border p-4 text-sm whitespace-pre-wrap">
          {team.run.final_summary}
        </div>
      )}
      {ArtifactPreview ? (
        <ArtifactPreview artifacts={entries} finalIds={team.run.final_artifact_ids ?? []} />
      ) : (
        entries.map((artifact) => (
          <article key={artifact.id} className="border-hair bg-card rounded-xl border p-3">
            <strong className="text-sm">
              {artifact.name || artifact.title || artifact.path || artifact.id}
            </strong>
            <p className="text-n600 mt-2 text-xs whitespace-pre-wrap">
              {artifact.summary || artifact.content}
            </p>
          </article>
        ))
      )}
      {artifacts.error && (
        <p role="alert" className="text-danger text-xs">
          {artifacts.error.message}
        </p>
      )}
      {artifacts.data?.pages[0]?.items.length === 0 && (
        <p className="text-n600 text-sm">{t("noArtifacts")}</p>
      )}
      {artifacts.hasNextPage && (
        <button
          type="button"
          onClick={() => void artifacts.fetchNextPage()}
          disabled={artifacts.isFetchingNextPage}
          className="text-a700 text-sm"
        >
          {t("loadMore")}
        </button>
      )}
    </div>
  )
}

export function TeamPanel({
  sessionId,
  ArtifactPreview,
}: {
  sessionId: string | null
  ArtifactPreview?: ComponentType<TeamArtifactPreviewProps>
}) {
  const { t } = useTranslation("teams")
  const runs = useTeamRuns({ session_id: sessionId ?? undefined }, !!sessionId)
  const [chosen, setChosen] = useState("")
  const entries = runs.data?.pages.flatMap((page) => page.items) ?? []
  const id = entries.find((run) => run.id === chosen)?.id ?? entries[0]?.id ?? null
  const query = useTeamRun(id)
  const [tab, setTab] = useState<Tab>("members")
  const [selected, setSelected] = useState<string | null>(null)
  const [pair, setPair] = useState("")
  const [focusTask, setFocusTask] = useState<string | null>(null)
  const team = query.data
  if (runs.isLoading || query.isLoading)
    return (
      <div className="flex flex-1 items-center justify-center">
        <Spinner />
      </div>
    )
  if (!team)
    return (
      <div className="text-n600 flex flex-1 flex-col items-center justify-center gap-3 p-6 text-sm">
        <Users className="size-6" />
        <p>{query.error?.message || t("noRun")}</p>
        <Link to={paths.agents} className="text-a700">
          {t("manageTeams")}
        </Link>
      </div>
    )
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="px-4 pb-3">
        <div className="flex items-center gap-2">
          <h2 className="min-w-0 flex-1 truncate text-sm font-medium">{team.run.title}</h2>
          <span className="text-n600 text-xs">{t(`state.${team.run.state}`)}</span>
        </div>
        {entries.length > 1 && (
          <select
            aria-label={t("selectRun")}
            className="bg-card border-hair mt-2 max-w-full rounded-lg border p-1 text-xs"
            value={id ?? ""}
            onChange={(event) => setChosen(event.target.value)}
          >
            {entries.map((run) => (
              <option key={run.id} value={run.id}>
                {run.title}
              </option>
            ))}
          </select>
        )}
        <div className="mt-3">
          <TeamControls team={team} />
          {terminalTeam(team.run.state) && <SaveTeamConfiguration team={team} />}
        </div>
        <div className="mt-2">
          <TeamRunAttention team={team} notices />
        </div>
      </div>
      <div role="tablist" aria-label={t("panelTabs")} className="border-hair flex gap-1 border-b px-3 pb-3">
        {TABS.map((item) => (
          <button
            key={item}
            type="button"
            role="tab"
            aria-selected={tab === item}
            onClick={() => setTab(item)}
            className={cn(
              "rounded-full px-3 py-1.5 text-xs",
              tab === item ? "bg-ink text-bg" : "text-n600 hover:bg-hairsoft",
            )}
          >
            {t(`tabs.${item}`)}
          </button>
        ))}
      </div>
      <div role="tabpanel" className="scr min-h-0 flex-1 overflow-auto p-4">
        {tab === "members" && (
          <Roster
            team={team}
            selected={selected}
            onSelect={setSelected}
            onTask={(taskId) => {
              setFocusTask(taskId)
              setTab("tasks")
            }}
            onMessages={(value) => {
              setPair(value)
              setTab("messages")
            }}
          />
        )}
        {tab === "tasks" && <TaskList id={team.id} team={team} focusTask={focusTask} />}
        {tab === "messages" && <Messages team={team} pair={pair} setPair={setPair} />}
        {tab === "artifacts" && <Artifacts team={team} ArtifactPreview={ArtifactPreview} />}
        {tab === "usage" && <TeamUsagePanel team={team} />}
      </div>
    </div>
  )
}
