// The 全选 / 筛选 / 排序 strip under the list title, mirroring DEEIX's file
// pane. Purely presentational — open/close is the only local state.
import { useState } from "react"
import { useTranslation } from "react-i18next"
import { ArrowDownUp, Check, Funnel, SquareDashed, SquareDashedMousePointer, Trash2 } from "lucide-react"
import { Menu } from "@/shared/ui/Menu"
import { cn } from "@/shared/lib/cn"
import { KIND_FILTERS, KIND_LABEL, SORT_LABEL, SORT_OPTIONS } from "../constants"
import type { KindFilter, ResourceSort } from "../types"

interface Props {
  kind: KindFilter
  sort: ResourceSort
  selectedCount: number
  selectAllDisabled: boolean
  onKind: (kind: KindFilter) => void
  onSort: (sort: ResourceSort) => void
  onSelectAll: () => void
  onClearSelection: () => void
  onDeleteSelected: () => void
}

const CHIP =
  "flex h-7 items-center gap-1 rounded-full px-2 text-2xs text-n700 hover:bg-hairsoft disabled:opacity-40"

function Option({ label, active, onClick }: { label: string; active: boolean; onClick: () => void }) {
  return (
    <button
      type="button"
      role="menuitemradio"
      aria-checked={active}
      onClick={onClick}
      className={cn(
        "flex items-center gap-2 rounded-lg px-2.5 py-1.5 text-start text-xs",
        active ? "bg-n200 text-ink" : "text-n800 hover:bg-hairsoft",
      )}
    >
      <span className="min-w-0 flex-1 truncate">{label}</span>
      {active && <Check className="size-3 flex-none" strokeWidth={2.4} />}
    </button>
  )
}

export function ListToolbar({
  kind,
  sort,
  selectedCount,
  selectAllDisabled,
  onKind,
  onSort,
  onSelectAll,
  onClearSelection,
  onDeleteSelected,
}: Props) {
  const { t } = useTranslation("resources")
  const [open, setOpen] = useState<"kind" | "sort" | null>(null)
  const selecting = selectedCount > 0

  return (
    <div className="flex items-center gap-0.5 px-1.5 pb-1">
      <button
        type="button"
        className={CHIP}
        disabled={!selecting && selectAllDisabled}
        onClick={selecting ? onClearSelection : onSelectAll}
      >
        {selecting ? (
          <SquareDashed className="size-3 flex-none" strokeWidth={2} />
        ) : (
          <SquareDashedMousePointer className="size-3 flex-none" strokeWidth={2} />
        )}
        {selecting ? t("actions.clearSelection") : t("actions.selectAll")}
      </button>

      <div className="relative">
        <button
          type="button"
          className={cn(CHIP, kind !== "all" && "bg-n200 text-ink")}
          onClick={() => setOpen((v) => (v === "kind" ? null : "kind"))}
        >
          <Funnel className="size-3 flex-none" strokeWidth={2} />
          {kind === "all" ? t("actions.filter") : t(KIND_LABEL[kind])}
        </button>
        <Menu open={open === "kind"} onClose={() => setOpen(null)} className="start-0 top-8 w-36">
          {KIND_FILTERS.map((value) => (
            <Option
              key={value}
              label={t(KIND_LABEL[value])}
              active={value === kind}
              onClick={() => {
                onKind(value)
                setOpen(null)
              }}
            />
          ))}
        </Menu>
      </div>

      <div className="relative">
        <button type="button" className={CHIP} onClick={() => setOpen((v) => (v === "sort" ? null : "sort"))}>
          <ArrowDownUp className="size-3 flex-none" strokeWidth={2} />
          {t("actions.sort")}
        </button>
        <Menu open={open === "sort"} onClose={() => setOpen(null)} className="start-0 top-8 w-32">
          {SORT_OPTIONS.map((value) => (
            <Option
              key={value}
              label={t(SORT_LABEL[value])}
              active={value === sort}
              onClick={() => {
                onSort(value)
                setOpen(null)
              }}
            />
          ))}
        </Menu>
      </div>

      {selecting && (
        <button type="button" className={cn(CHIP, "text-dangerink")} onClick={onDeleteSelected}>
          <Trash2 className="size-3 flex-none" strokeWidth={2} />
          {t("actions.deleteCount", { count: selectedCount })}
        </button>
      )}
    </div>
  )
}
