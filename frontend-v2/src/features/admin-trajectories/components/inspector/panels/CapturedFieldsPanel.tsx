import { useTranslation } from "react-i18next"
import { FIELD_LABELS, type TabFields } from "../../../constants/inspector"
import { labelKey } from "../../../constants/labels"
import { isPayloadEnvelope } from "../../../types/protocol"
import { fieldState } from "../../../utils/availability"
import { AvailabilityNote } from "../AvailabilityNote"
import { ContentActions } from "../ContentActions"
import { Section } from "../Field"
import { NS, type InspectedRecord } from "../types"
import { ValueView } from "../ValueView"

interface CapturedFieldsPanelProps {
  record: InspectedRecord
  fields: TabFields
}

function has(record: InspectedRecord, key: string): boolean {
  return Object.prototype.hasOwnProperty.call(record.data ?? {}, key)
}

/**
 * The captured values a tab is about. Fields the record may still produce are
 * always listed (pending / not recorded); optional ones appear only when the
 * producer captured them, so absence of an optional error is not "not recorded".
 */
export function CapturedFieldsPanel({ record, fields }: CapturedFieldsPanelProps) {
  const { t } = useTranslation(NS)
  const shown = fields.keys.filter((key) => has(record, key) || fields.awaiting?.includes(key))
  if (!shown.length) return <AvailabilityNote state="not_recorded" />
  return (
    <div className="flex flex-col gap-4">
      {shown.map((key) => {
        const value = record.data?.[key]
        const structured =
          has(record, key) && typeof value === "object" && value !== null && !isPayloadEnvelope(value)
        return (
          <Section
            key={key}
            title={t(labelKey(FIELD_LABELS, key, "field.other"), { value: key })}
            actions={
              structured ? (
                <ContentActions value={value} name={`${record.record_id}-${key}`} format="json" />
              ) : undefined
            }
          >
            <ValueView
              field={fieldState(record, key, fields.awaiting?.includes(key))}
              format={fields.markdown?.includes(key) ? "markdown" : "auto"}
            />
          </Section>
        )
      })}
    </div>
  )
}
