import { useRef, type KeyboardEvent } from "react"
import { useTranslation } from "react-i18next"
import { cn } from "@/shared/lib/cn"
import { TAB_LABELS } from "../../constants/labels"
import type { TabId } from "../../utils/tabs"
import { panelDomId, tabDomId } from "./tabIds"
import { NS } from "./types"

interface InspectorTabsProps {
  tabs: readonly TabId[]
  active: TabId
  onChange: (tab: TabId) => void
  idBase: string
}

/** Tabs for the selected record's kind. Arrow keys move and activate; Home/End jump. */
export function InspectorTabs({ tabs, active, onChange, idBase }: InspectorTabsProps) {
  const { t } = useTranslation(NS)
  const list = useRef<HTMLDivElement>(null)
  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const index = tabs.indexOf(active)
    const targets: Record<string, number> = {
      ArrowRight: (index + 1) % tabs.length,
      ArrowLeft: (index - 1 + tabs.length) % tabs.length,
      Home: 0,
      End: tabs.length - 1,
    }
    if (!(event.key in targets)) return
    event.preventDefault()
    const next = tabs[targets[event.key]]
    onChange(next)
    const button = [...(list.current?.querySelectorAll<HTMLButtonElement>("[role=tab]") ?? [])].find(
      (item) => item.id === tabDomId(idBase, next),
    )
    button?.focus()
  }
  return (
    <div
      ref={list}
      role="tablist"
      aria-label={t("inspector.tabs")}
      onKeyDown={onKeyDown}
      className="border-hair flex gap-1 overflow-x-auto border-b px-3"
      data-testid="trajectory-inspector-tabs"
    >
      {tabs.map((tab) => (
        <button
          key={tab}
          id={tabDomId(idBase, tab)}
          type="button"
          role="tab"
          aria-selected={tab === active}
          aria-controls={panelDomId(idBase)}
          tabIndex={tab === active ? 0 : -1}
          onClick={() => onChange(tab)}
          className={cn(
            "-mb-px flex-none border-b-2 px-2 py-2 text-xs whitespace-nowrap",
            tab === active
              ? "border-ink text-ink font-medium"
              : "text-n600 hover:text-ink border-transparent",
          )}
          data-testid={`trajectory-tab-${tab}`}
        >
          {t(TAB_LABELS[tab])}
        </button>
      ))}
    </div>
  )
}
