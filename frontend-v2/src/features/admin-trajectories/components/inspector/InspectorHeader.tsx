import { Fragment } from "react"
import { useTranslation } from "react-i18next"
import { ChevronRight } from "lucide-react"
import { Spinner } from "@/shared/ui/Spinner"
import { KIND_LABELS, labelKey, ORDINAL_LABELS } from "../../constants/labels"
import type { ViewRecord } from "../../utils/view"
import { KindBadge } from "../session/KindBadge"
import { positionOf } from "../session/ordinals"
import { Status } from "../StatusPills"
import { CopyButton } from "./CopyButton"
import { useInspector } from "./context"
import { NS } from "./types"

interface InspectorHeaderProps {
  record: ViewRecord
  chain: readonly ViewRecord[]
  /** Details are being refreshed to the shown position. */
  updating: boolean
}

const NUMBERED = new Set(["turn", "run", "step", "request"])

/** "Tool · bash · Completed" with its breadcrumb (Agent › Turn › Run › Step › Request) from persisted relations. */
export function InspectorHeader({ record, chain, updating }: InspectorHeaderProps) {
  const { t } = useTranslation(NS)
  const { select, ordinals } = useInspector()
  const crumb = (item: ViewRecord) => {
    if (NUMBERED.has(item.kind)) {
      const part = positionOf(item, ordinals).find((entry) => entry.scope === item.kind)
      if (part) return t(labelKey(ORDINAL_LABELS, part.scope, "position.other"), { n: part.value })
    }
    return item.kind === "agent"
      ? item.title
      : t(labelKey(KIND_LABELS, item.kind, "kind.other"), { value: item.kind })
  }
  return (
    <header
      className="border-hair flex flex-col gap-1.5 border-b px-4 py-3"
      data-testid="trajectory-inspector-header"
    >
      {chain.length > 0 && (
        <nav
          aria-label={t("inspector.breadcrumb")}
          className="text-n600 text-2xs flex flex-wrap items-center gap-0.5"
        >
          {chain.map((item) => (
            <Fragment key={item.record_id}>
              <button
                type="button"
                onClick={() => select(item.record_id)}
                className="hover:text-ink max-w-40 truncate hover:underline"
              >
                {crumb(item)}
              </button>
              <ChevronRight size={10} aria-hidden className="rtl:rotate-180" />
            </Fragment>
          ))}
        </nav>
      )}
      <div className="flex min-w-0 items-center gap-2">
        <KindBadge kind={record.kind} />
        <h3 className="text-ink min-w-0 flex-1 truncate text-sm font-medium" title={record.title}>
          {record.title}
        </h3>
        {updating && <Spinner className="size-3.5" />}
        <Status scope="record" value={record.status} reason={record.status_reason} />
      </div>
      <p className="text-n500 text-2xs flex min-w-0 items-center gap-1 font-mono">
        <span className="truncate">{record.record_id}</span>
        <CopyButton text={record.record_id} label={t("inspector.copyRecordId")} className="size-5" />
      </p>
    </header>
  )
}
