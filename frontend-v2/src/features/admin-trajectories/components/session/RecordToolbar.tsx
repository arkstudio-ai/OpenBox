import { useId } from "react"
import { useTranslation } from "react-i18next"
import { Search } from "lucide-react"
import { cn } from "@/shared/lib/cn"
import { KIND_LABELS, labelKey, RECORD_STATUS_LABELS } from "../../constants/labels"
import { hasFilters, type RecordFilters } from "../../utils/view"
import { FilterMenu } from "./FilterMenu"

export interface ToolbarOptions {
  kinds: readonly string[]
  statuses: readonly string[]
  agents: ReadonlyArray<{ id: string; label: string }>
}

interface RecordToolbarProps {
  filters: RecordFilters
  options: ToolbarOptions
  onFilters: (patch: Partial<RecordFilters>) => void
  counts: { shown: number; total: number }
  searchOpen: boolean
  onToggleSearch: () => void
}

/**
 * Narrowing the table: quick text over the records at this position, type,
 * status and agent. Filters change what is listed, never the replay stream.
 * Full-range server search opens separately.
 */
export function RecordToolbar({
  filters,
  options,
  onFilters,
  counts,
  searchOpen,
  onToggleSearch,
}: RecordToolbarProps) {
  const { t } = useTranslation("admin-trajectories")
  const agentId = useId()
  return (
    <div
      role="toolbar"
      aria-label={t("toolbar.title")}
      className="border-hair flex flex-wrap items-center gap-2 border-b px-3 py-2"
      data-testid="trajectory-toolbar"
    >
      <input
        type="search"
        value={filters.text}
        onChange={(event) => onFilters({ text: event.target.value })}
        placeholder={t("toolbar.findPlaceholder")}
        aria-label={t("toolbar.findPlaceholder")}
        className="border-hair bg-bg text-ink placeholder:text-n500 w-40 rounded-full border px-3 py-1 text-xs sm:w-52"
        data-testid="trajectory-find"
      />
      <FilterMenu
        label={t("toolbar.kind")}
        values={options.kinds}
        selected={filters.kinds}
        labelFor={(kind) => t(labelKey(KIND_LABELS, kind, "kind.other"), { value: kind })}
        onChange={(kinds) => onFilters({ kinds })}
      />
      <FilterMenu
        label={t("toolbar.status")}
        values={options.statuses}
        selected={filters.statuses}
        labelFor={(status) => t(labelKey(RECORD_STATUS_LABELS, status, "status.other"), { value: status })}
        onChange={(statuses) => onFilters({ statuses })}
      />
      <label htmlFor={agentId} className="sr-only">
        {t("toolbar.agent")}
      </label>
      <select
        id={agentId}
        value={filters.agentId ?? ""}
        onChange={(event) => onFilters({ agentId: event.target.value || null })}
        className="border-hair bg-bg text-ink max-w-44 rounded-full border px-2 py-1 text-xs"
        data-testid="trajectory-agent-filter"
      >
        <option value="">{t("toolbar.allAgents")}</option>
        {options.agents.map((agent) => (
          <option key={agent.id} value={agent.id}>
            {agent.label}
          </option>
        ))}
      </select>
      {hasFilters(filters) && (
        <button
          type="button"
          className="text-a700 text-xs hover:underline"
          onClick={() => onFilters({ kinds: [], statuses: [], agentId: null, text: "" })}
        >
          {t("toolbar.clear")}
        </button>
      )}
      <span className="flex-1" />
      <span className="text-n600 text-2xs" role="status" data-testid="trajectory-row-count">
        {t("toolbar.count", { shown: counts.shown, total: counts.total })}
      </span>
      <button
        type="button"
        aria-pressed={searchOpen}
        onClick={onToggleSearch}
        className={cn(
          "border-hair inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs",
          searchOpen ? "bg-ink text-bg" : "hover:bg-hairsoft text-n700",
        )}
        data-testid="trajectory-search-toggle"
      >
        <Search size={12} aria-hidden />
        {t("toolbar.fullSearch")}
      </button>
    </div>
  )
}
