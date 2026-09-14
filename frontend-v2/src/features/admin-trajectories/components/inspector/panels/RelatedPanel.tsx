import { useTranslation } from "react-i18next"
import { labelKey, RELATION_LABELS } from "../../../constants/labels"
import { relationsFor } from "../../../utils/relations"
import { AvailabilityNote } from "../AvailabilityNote"
import { Section } from "../Field"
import { RecordLink } from "../RecordLink"
import { useInspector } from "../context"
import { NS, type PanelProps } from "../types"

const CHILD_LIMIT = 200

/** Records this one names, records nested under it, and runs that resumed it — all by persisted ids. */
export function RelatedPanel({ record }: PanelProps) {
  const { t } = useTranslation(NS)
  const { records, tree } = useInspector()
  const relations = relationsFor(record, records)
  const children = tree.nodes.get(record.record_id)?.children ?? []
  const resumedBy =
    record.kind === "run" && record.run_id
      ? Object.values(records).filter(
          (item) => item.kind === "resume" && item.data?.resume_of_run_id === record.run_id,
        )
      : []
  const empty = !relations.length && !children.length && !resumedBy.length
  return (
    <div className="flex flex-col gap-4" data-testid="trajectory-related">
      {empty && <AvailabilityNote state="empty" />}
      {relations.length > 0 && (
        <Section title={t("related.links")}>
          <ul className="flex flex-col gap-1">
            {relations.map((relation) => (
              <li key={relation.recordId} className="flex items-center gap-2 text-xs">
                <span className="text-n500 w-28 flex-none">
                  {t(labelKey(RELATION_LABELS, relation.kind, "relation.other"))}
                </span>
                <RecordLink recordId={relation.recordId} />
              </li>
            ))}
          </ul>
        </Section>
      )}
      {resumedBy.length > 0 && (
        <Section title={t("related.resumedBy")}>
          {resumedBy.map((item) => (
            <RecordLink key={item.record_id} recordId={item.record_id} />
          ))}
        </Section>
      )}
      {children.length > 0 && (
        <Section title={t("related.children", { count: children.length })}>
          <ul className="flex flex-col gap-1">
            {children.slice(0, CHILD_LIMIT).map((id) => (
              <li key={id}>
                <RecordLink recordId={id} />
              </li>
            ))}
          </ul>
          {children.length > CHILD_LIMIT && (
            <p className="text-n500 text-xs">
              {t("related.moreInTable", { count: children.length - CHILD_LIMIT })}
            </p>
          )}
        </Section>
      )}
    </div>
  )
}
