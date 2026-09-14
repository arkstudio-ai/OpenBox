import { useTranslation } from "react-i18next"
import { ArrowUpRight } from "lucide-react"
import { KIND_LABELS, labelKey } from "../../constants/labels"
import { useInspector } from "./context"
import { NS } from "./types"

interface RecordLinkProps {
  recordId: string
  /** Replaces the default "kind · title" text. */
  label?: string
}

/**
 * Navigation to another record of the same trajectory. It only ever selects;
 * a record that does not exist at the shown position is named, not linked.
 */
export function RecordLink({ recordId, label }: RecordLinkProps) {
  const { t } = useTranslation(NS)
  const { records, select } = useInspector()
  const target = records[recordId]
  if (!target) {
    return (
      <span className="text-n500 font-mono text-xs" title={t("relation.notAtPosition")}>
        {label ?? recordId}
      </span>
    )
  }
  const text =
    label ??
    t("relation.label", {
      kind: t(labelKey(KIND_LABELS, target.kind, "kind.other"), { value: target.kind }),
      title: target.title,
    })
  return (
    <button
      type="button"
      onClick={() => select(recordId)}
      className="text-a700 inline-flex max-w-full items-center gap-1 text-start text-xs hover:underline"
      data-testid="trajectory-record-link"
    >
      <span className="truncate">{text}</span>
      <ArrowUpRight size={12} aria-hidden className="flex-none" />
    </button>
  )
}
