import { useEffect, useRef, useState } from "react"
import { useTranslation } from "react-i18next"
import { useTeamCollection } from "../api/teams"
import { taskDisplayState, type TeamAttempt, type TeamSnapshot, type TeamTask } from "../types"

export function TeamTaskRow({
  task,
  team,
  focused,
}: {
  task: TeamTask
  team: TeamSnapshot
  focused: boolean
}) {
  const { t } = useTranslation("teams")
  const [expanded, setExpanded] = useState(false)
  const element = useRef<HTMLDetailsElement>(null)
  const attempts = useTeamCollection<TeamAttempt>(
    team.id,
    "attempts",
    { task_id: task.id },
    expanded || focused,
  )
  useEffect(() => {
    if (focused) element.current?.scrollIntoView?.({ block: "nearest" })
  }, [focused])
  return (
    <details
      ref={element}
      open={focused || undefined}
      onToggle={(event) => setExpanded(event.currentTarget.open)}
      className="bg-card border-hair rounded-xl border p-3"
    >
      <summary className="cursor-pointer text-sm">
        <span>{task.title}</span>
        <span className="text-n600 ms-2 text-xs">{t(`state.${taskDisplayState(task, team.members)}`)}</span>
      </summary>
      <div className="text-n600 mt-3 space-y-2 text-xs">
        <p>{task.description}</p>
        <p>
          {t("owner")}: {team.members.find((member) => member.id === task.owner_member_id)?.name}
        </p>
        <p>
          {t("expectedOutput")}: {task.expected_output}
        </p>
        {task.dependencies.length > 0 && (
          <p>
            {t("dependencies")}:{" "}
            {task.dependencies
              .map((dependency) => team.tasks.find((entry) => entry.id === dependency)?.title ?? dependency)
              .join(" · ")}
          </p>
        )}
        {task.blocked_reason && <p className="text-a700">{task.blocked_reason}</p>}
        {attempts.data?.pages
          .flatMap((page) => page.items)
          .map((attempt) => (
            <div key={attempt.id} className="border-hair border-t pt-2">
              <strong>{t("attempt", { count: attempt.number })}</strong>
              <span className="ms-2">{t(`state.${attempt.state}`)}</span>
              {attempt.implicit && <span className="ms-2">{t("implicit")}</span>}
              <p className="mt-1 whitespace-pre-wrap">{attempt.summary || attempt.error}</p>
            </div>
          ))}
        {attempts.hasNextPage && (
          <button
            type="button"
            disabled={attempts.isFetchingNextPage}
            onClick={() => void attempts.fetchNextPage()}
            className="text-a700 text-sm"
          >
            {t("moreAttempts")}
          </button>
        )}
        {attempts.error && (
          <p role="alert" className="text-danger">
            {attempts.error.message}
          </p>
        )}
      </div>
    </details>
  )
}
