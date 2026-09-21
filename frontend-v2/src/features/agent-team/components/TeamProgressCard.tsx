import { useState } from "react"
import { useTranslation } from "react-i18next"
import { Check, ChevronDown, Circle, Users } from "lucide-react"
import { TaskCardFrame } from "@/shared/ui/TaskCardFrame"
import { cn } from "@/shared/lib/cn"
import { useCopy } from "@/shared/hooks/useCopy"
import { useTeamControl, useTeamRun, useTeamRuns } from "../api/teams"
import { taskDisplayState, terminalTeam, type TeamSnapshot } from "../types"
import { SaveTeamConfiguration } from "./SaveTeamConfiguration"
import { TeamGrantControl } from "./TeamGrantDialog"

export function TeamRunAttention({ team, notices = false }: { team: TeamSnapshot; notices?: boolean }) {
  const { t } = useTranslation("teams")
  const visible = team.notices.filter((notice) => notice.message || notice.reason)
  return (
    <div className="text-a700 space-y-2 text-xs">
      {team.run.capacity_retry_at && ["running", "waiting"].includes(team.run.state) && (
        <p role="status">{t("capacityWait")}</p>
      )}
      {team.run.pause_reason && (
        <p role="status">{t(`reason.${team.run.pause_reason}`, { defaultValue: team.run.pause_reason })}</p>
      )}
      {team.run.failure_reason && <p role="status">{team.run.failure_reason}</p>}
      {notices && visible.length > 0 && (
        <details>
          <summary className="cursor-pointer">{t("attentionCount", { count: visible.length })}</summary>
          <ul className="mt-2 space-y-2">
            {visible.map((notice, index) => (
              <li key={notice.id ?? index}>
                {t(`notice.${notice.code}`, { defaultValue: notice.message || notice.reason || "" })}
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  )
}

export function TeamControls({ team }: { team: TeamSnapshot }) {
  const { t } = useTranslation("teams")
  const control = useTeamControl(team.id)
  const state = team.run.state
  const transitional = ["pausing", "canceling", "completing", "provisioning"].includes(state)
  if (terminalTeam(state)) return null
  return (
    <div className="flex flex-wrap items-center gap-3">
      {["running", "waiting", "paused"].includes(state) && <TeamGrantControl team={team} />}
      {!transitional && (
        <button
          type="button"
          disabled={control.isPending}
          onClick={() =>
            control.mutate({ action: state === "paused" ? "resume" : "pause", revision: team.run.revision })
          }
          className="text-a700 text-xs disabled:opacity-40"
        >
          {t(state === "paused" ? "resume" : "pause")}
        </button>
      )}
      {state !== "canceling" && (
        <button
          type="button"
          disabled={control.isPending}
          onClick={() => control.mutate({ action: "cancel", revision: team.run.revision })}
          className="text-n600 text-xs disabled:opacity-40"
        >
          {t("cancelRun")}
        </button>
      )}
      {control.error && (
        <span className="text-danger text-xs" role="alert">
          {control.error.message}
        </span>
      )}
    </div>
  )
}

function TeamChangesLink({ team, onChanges }: { team: TeamSnapshot; onChanges?: () => void }) {
  const { t } = useTranslation("teams")
  const boundaries = team.run.workspace_snapshots
  if (!onChanges || !terminalTeam(team.run.state) || !boundaries?.start?.hash || !boundaries?.end?.hash)
    return null
  return (
    <button type="button" onClick={onChanges} className="text-a700 text-xs">
      {t("viewChanges")}
    </button>
  )
}

export function TeamProgressCard({
  sessionId,
  onDetails,
  onChanges,
  enabled = true,
  runId,
}: {
  sessionId: string
  onDetails: () => void
  onChanges?: () => void
  enabled?: boolean
  runId?: string
}) {
  const runs = useTeamRuns({ session_id: sessionId }, enabled && !!sessionId && !runId)
  const id = runId ?? runs.data?.pages[0]?.items[0]?.id ?? null
  const query = useTeamRun(id)
  const { t } = useTranslation("teams")
  const [expanded, setExpanded] = useState(true)
  const { copied, copy } = useCopy()
  const team = query.data
  if (!team) return null
  const done = team.completed_task_count ?? team.tasks.filter((task) => task.state === "succeeded").length
  const ended = terminalTeam(team.run.state)
  return (
    <TaskCardFrame className="mx-auto max-w-190">
      <div className="flex flex-wrap items-center gap-2">
        <Users className="text-a700 size-4" />
        <button
          type="button"
          aria-expanded={expanded}
          onClick={() => setExpanded(!expanded)}
          className="flex min-w-0 flex-1 items-center gap-2 text-start text-sm font-medium"
        >
          <span className="truncate">{team.run.title}</span>
          <span className="text-n600 shrink-0 text-xs">
            {t("taskCount", { done, total: team.task_count })}
          </span>
          <ChevronDown className={cn("size-3.5", expanded && "rotate-180")} />
        </button>
        <span className="bg-hairsoft rounded-full px-2 py-0.5 text-xs">{t(`state.${team.run.state}`)}</span>
      </div>
      {expanded && (
        <>
          <div
            className="bg-hairsoft my-3 h-1 overflow-hidden rounded-full"
            role="progressbar"
            aria-label={t("progress")}
            aria-valuemin={0}
            aria-valuemax={team.task_count || 1}
            aria-valuenow={done}
          >
            <div
              className="bg-accent h-full transition-[width] motion-reduce:transition-none"
              style={{ width: `${team.task_count ? (done / team.task_count) * 100 : 0}%` }}
            />
          </div>
          <ul className="space-y-2">
            {team.tasks.slice(0, 8).map((task) => (
              <li key={task.id} className="flex items-center gap-2 text-sm">
                {task.state === "succeeded" ? (
                  <Check className="text-s600 size-4 flex-none" />
                ) : (
                  <Circle
                    className={cn("size-4 flex-none", task.state === "running" ? "text-a700" : "text-n500")}
                  />
                )}
                <span className="min-w-0 flex-1 truncate">{task.title}</span>
                <span className="text-n600 max-w-24 truncate text-xs">
                  {team.members.find((member) => member.id === task.owner_member_id)?.name}
                </span>
                <span className="text-n600 text-xs">
                  {t(`state.${taskDisplayState(task, team.members)}`)}
                </span>
              </li>
            ))}
          </ul>
          <div className="mt-3">
            <TeamRunAttention team={team} />
          </div>
          <div className="text-n600 mt-3 flex flex-wrap items-center gap-3 text-xs">
            <span>{t("membersValue", { count: team.members.length })}</span>
          </div>
        </>
      )}
      <div className="mt-3 flex flex-wrap items-center gap-4">
        <button type="button" onClick={onDetails} className="text-a700 text-xs">
          {t("details")}
        </button>
        <TeamControls team={team} />
        <TeamChangesLink team={team} onChanges={onChanges} />
        {ended && team.run.final_summary && (
          <button type="button" onClick={() => copy(team.run.final_summary!)} className="text-a700 text-xs">
            {t(copied ? "finalCopied" : "copyFinal")}
          </button>
        )}
        {ended && <SaveTeamConfiguration team={team} />}
      </div>
    </TaskCardFrame>
  )
}
