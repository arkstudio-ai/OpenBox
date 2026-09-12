import { useTranslation } from "react-i18next"
import { ArrowUpRight } from "lucide-react"
import { cn } from "@/shared/lib/cn"
import type { AgentTreeNode } from "../../utils/view"
import { Status } from "../StatusPills"
import { shortId } from "./format"

interface AgentTreePanelProps {
  nodes: readonly AgentTreeNode[]
  activeAgentId: string | null
  onFilter: (agentId: string | null) => void
  onSelect: (recordId: string) => void
  className?: string
}

interface AgentItemProps {
  node: AgentTreeNode
  depth: number
  activeAgentId: string | null
  onFilter: (agentId: string | null) => void
  onSelect: (recordId: string) => void
}

function AgentItem({ node, depth, activeAgentId, onFilter, onSelect }: AgentItemProps) {
  const { t } = useTranslation("admin-trajectories")
  const name = node.name ?? t(depth === 0 && !node.recordId ? "agents.main" : "agents.unnamed")
  const active = activeAgentId === node.agentId
  return (
    <li>
      <div
        className={cn("flex min-w-0 items-center gap-1 rounded-md pe-1", active && "bg-a100")}
        style={{ paddingInlineStart: `${depth * 0.75}rem` }}
      >
        <button
          type="button"
          aria-pressed={active}
          onClick={() => onFilter(active ? null : node.agentId)}
          className="hover:text-ink text-n800 flex min-w-0 flex-1 flex-col items-start px-1.5 py-1 text-start"
          title={t("agents.filterBy", { name })}
          data-testid="trajectory-agent-node"
          data-agent-id={node.agentId}
        >
          <span className="w-full truncate text-xs">{name}</span>
          <span className="text-n500 text-2xs w-full truncate font-mono">{shortId(node.agentId)}</span>
        </button>
        {node.status && <Status scope="record" value={node.status} />}
        {node.recordId && (
          <button
            type="button"
            onClick={() => node.recordId && onSelect(node.recordId)}
            aria-label={t("agents.open", { name })}
            title={t("agents.open", { name })}
            className="text-n500 hover:text-ink hover:bg-hairsoft rounded p-0.5"
          >
            <ArrowUpRight size={12} aria-hidden />
          </button>
        )}
      </div>
      {node.children.length > 0 && (
        <ul>
          {node.children.map((child) => (
            <AgentItem
              key={child.agentId}
              node={child}
              depth={depth + 1}
              activeAgentId={activeAgentId}
              onFilter={onFilter}
              onSelect={onSelect}
            />
          ))}
        </ul>
      )}
    </li>
  )
}

/** Main and delegated agents at the shown position, nested by recorded parent ids; choosing one filters the table. */
export function AgentTreePanel({ nodes, activeAgentId, onFilter, onSelect, className }: AgentTreePanelProps) {
  const { t } = useTranslation("admin-trajectories")
  return (
    <nav
      aria-label={t("agents.title")}
      className={cn("flex min-h-0 flex-col gap-1 overflow-auto p-2", className)}
      data-testid="trajectory-agent-tree"
    >
      <h3 className="text-n700 text-2xs px-1.5 font-medium uppercase">{t("agents.title")}</h3>
      <button
        type="button"
        aria-pressed={activeAgentId === null}
        onClick={() => onFilter(null)}
        className={cn(
          "text-n800 rounded-md px-1.5 py-1 text-start text-xs",
          activeAgentId === null ? "bg-a100" : "hover:bg-hairsoft",
        )}
      >
        {t("toolbar.allAgents")}
      </button>
      {!nodes.length && <p className="text-n500 px-1.5 text-xs">{t("agents.none")}</p>}
      <ul className="flex flex-col">
        {nodes.map((node) => (
          <AgentItem
            key={node.agentId}
            node={node}
            depth={0}
            activeAgentId={activeAgentId}
            onFilter={onFilter}
            onSelect={onSelect}
          />
        ))}
      </ul>
    </nav>
  )
}
