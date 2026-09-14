import { useMemo, useState } from "react"
import { useTranslation } from "react-i18next"
import { agentScope, buildTree, flattenTree } from "../../../utils/view"
import { KindBadge } from "../../session/KindBadge"
import { Status } from "../../StatusPills"
import { AvailabilityNote } from "../AvailabilityNote"
import { RecordLink } from "../RecordLink"
import { useInspector } from "../context"
import { NS, type PanelProps } from "../types"

const PAGE = 300

/** Everything this agent and the agents it spawned did, nested by recorded relations. */
export function AgentTreeTab({ record }: PanelProps) {
  const { t } = useTranslation(NS)
  const { records } = useInspector()
  const [limit, setLimit] = useState(PAGE)
  const rows = useMemo(() => {
    if (!record.agent_id) return []
    const all = Object.values(records)
    const scope = agentScope(all, record.agent_id)
    const subset = all.filter(
      (item) => item.record_id !== record.record_id && item.agent_id && scope.has(item.agent_id),
    )
    return flattenTree(buildTree(subset), {})
  }, [record.agent_id, record.record_id, records])
  if (!record.agent_id) return <AvailabilityNote state="not_recorded" />
  if (!rows.length) return <AvailabilityNote state="empty" />
  return (
    <div className="flex flex-col gap-1" data-testid="trajectory-agent-subtree">
      <p className="text-n600 text-xs">{t("agent.subtreeCount", { count: rows.length })}</p>
      <ul className="flex flex-col">
        {rows.slice(0, limit).map((row) => (
          <li
            key={row.record.record_id}
            className="flex min-w-0 items-center gap-2 py-0.5"
            style={{ paddingInlineStart: `${row.depth * 0.9}rem` }}
          >
            <KindBadge kind={row.record.kind} className="w-24" />
            <span className="min-w-0 flex-1">
              <RecordLink recordId={row.record.record_id} label={row.record.title} />
            </span>
            <Status scope="record" value={row.record.status} />
          </li>
        ))}
      </ul>
      {rows.length > limit && (
        <button
          type="button"
          className="text-a700 w-fit text-xs hover:underline"
          onClick={() => setLimit((value) => value + PAGE)}
        >
          {t("common.showMore", { count: rows.length - limit })}
        </button>
      )}
    </div>
  )
}
