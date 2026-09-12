import { useTranslation } from "react-i18next"
import { TAB_FIELDS } from "../../../constants/inspector"
import { eqSeq } from "../../../utils/seq"
import { AvailabilityNote } from "../AvailabilityNote"
import { Section } from "../Field"
import { RecordLink } from "../RecordLink"
import { useInspector } from "../context"
import { NS, type PanelProps } from "../types"
import { CapturedFieldsPanel } from "./CapturedFieldsPanel"

/** Why the run stopped and which open operations were closed as "result unknown" at that point. */
export function InterruptImpactPanel({ record }: PanelProps) {
  const { t } = useTranslation(NS)
  const { records } = useInspector()
  const affected = Object.values(records).filter(
    (item) =>
      item.record_id !== record.record_id &&
      item.status === "unknown" &&
      item.end_seq !== null &&
      eqSeq(item.end_seq, record.start_seq) &&
      (!record.run_id || item.run_id === record.run_id),
  )
  return (
    <div className="flex flex-col gap-4">
      <CapturedFieldsPanel record={record} fields={TAB_FIELDS["interrupt.impact"]} />
      <Section title={t("interrupt.affected", { count: affected.length })}>
        {affected.length ? (
          <ul className="flex flex-col gap-1">
            {affected.map((item) => (
              <li key={item.record_id}>
                <RecordLink recordId={item.record_id} />
              </li>
            ))}
          </ul>
        ) : (
          <AvailabilityNote state="empty" />
        )}
        <p className="text-n600 text-xs">{t("interrupt.prefixNote")}</p>
      </Section>
    </div>
  )
}
