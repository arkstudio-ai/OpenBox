import { useTranslation } from "react-i18next"
import { FIELD_LABELS, SOURCE_FACT_KEYS } from "../../../constants/inspector"
import { CAPTURE_LEVEL_LABELS, labelKey } from "../../../constants/labels"
import { Field, FieldList } from "../Field"
import { InlineValue } from "../InlineValue"
import { RecordLink } from "../RecordLink"
import { NS, type PanelProps } from "../types"

/** Where a record came from: channel and actor, chat ids, the request and agent, and its event range. */
export function SourcePanel({ record }: PanelProps) {
  const { t } = useTranslation(NS)
  const data = record.data ?? {}
  const capture = typeof data.capture_level === "string" ? data.capture_level : null
  const sourceRequest = typeof data.source_request_id === "string" ? data.source_request_id : null
  return (
    <FieldList>
      {SOURCE_FACT_KEYS.filter((key) => key in data).map((key) => (
        <Field key={key} label={t(labelKey(FIELD_LABELS, key, "field.other"), { value: key })}>
          <InlineValue value={data[key]} />
        </Field>
      ))}
      {capture && (
        <Field label={t("field.captureLevel")}>
          {t(labelKey(CAPTURE_LEVEL_LABELS, capture, "captureLevel.other"), { value: capture })}
        </Field>
      )}
      {record.request_id && (
        <Field label={t("relation.request")}>
          <RecordLink recordId={`request:${record.request_id}`} label={record.request_id} />
        </Field>
      )}
      {sourceRequest && (
        <Field label={t("relation.sourceRequest")}>
          <RecordLink recordId={`request:${sourceRequest}`} label={sourceRequest} />
        </Field>
      )}
      {record.call_id && (
        <Field label={t("relation.tool")}>
          <RecordLink recordId={`tool:${record.call_id}`} label={record.call_id} />
        </Field>
      )}
      {record.agent_id && (
        <Field label={t("relation.agent")}>
          <RecordLink recordId={`agent:${record.agent_id}`} label={record.agent_id} />
        </Field>
      )}
      {record.message_id && <Field label={t("field.messageId")}>{record.message_id}</Field>}
      {record.part_id && <Field label={t("field.partId")}>{record.part_id}</Field>}
      {record.source_session_id && <Field label={t("source.session")}>{record.source_session_id}</Field>}
      {record.caused_by_event_id && <Field label={t("source.causedBy")}>{record.caused_by_event_id}</Field>}
      <Field label={t("source.eventRange")}>
        <span className="font-mono">
          {t("source.range", {
            start: record.start_seq,
            end: record.end_seq ?? t("source.open"),
            asOf: record.as_of_seq,
          })}
        </span>
      </Field>
    </FieldList>
  )
}
