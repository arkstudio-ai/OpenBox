import { useTranslation } from "react-i18next"
import { AVAILABILITY_LABELS, labelKey } from "../../../constants/labels"
import { fieldState, isClosed } from "../../../utils/availability"
import { stableText } from "../../../utils/diff"
import { isPlainObject } from "../../../utils/python"
import { ContentActions } from "../ContentActions"
import { Field, FieldList, Section } from "../Field"
import { InlineValue } from "../InlineValue"
import { JsonTree } from "../JsonTree"
import { RecordLink } from "../RecordLink"
import { useInspector } from "../context"
import { NS, type PanelProps } from "../types"
import { ValueView } from "../ValueView"

const HIGHLIGHTED_METADATA = ["exit_code", "signal", "truncated", "blocked", "rejected", "duration"] as const

/**
 * What the executor produced and, separately, what the model was given back.
 * They differ when output was trimmed or summarised; both stay inspectable,
 * along with errors, public metadata and anything the call produced.
 */
export function ToolResultPanel({ record }: PanelProps) {
  const { t } = useTranslation(NS)
  const { records } = useInspector()
  const data = record.data ?? {}
  const open = !isClosed(record)
  const output = fieldState(record, "output", true)
  const model = fieldState(record, "model_output", true)
  const same =
    output.state === "available" &&
    model.state === "available" &&
    stableText(output.value) === stableText(model.value)
  const metadata = isPlainObject(data.metadata) ? data.metadata : null
  const availability = typeof data.result_availability === "string" ? data.result_availability : null
  const all = Object.values(records)
  const artifacts = all.filter(
    (item) => item.kind === "artifact" && record.call_id && item.call_id === record.call_id,
  )
  const children = all.filter(
    (item) => item.kind === "tool" && record.call_id && item.parent_call_id === record.call_id,
  )
  return (
    <div className="flex flex-col gap-4" data-testid="trajectory-tool-result">
      {!open && !record.started_at && (
        <p className="bg-hairsoft text-n700 rounded-lg px-3 py-2 text-xs">
          {t("tool.rejectedBeforeExecution")}
        </p>
      )}
      {availability && (
        <p className="text-n600 text-xs">
          {t("tool.resultAvailability", {
            value: t(labelKey(AVAILABILITY_LABELS, availability, "availability.other"), {
              value: availability,
            }),
          })}
        </p>
      )}
      {data.error !== undefined && data.error !== null && (
        <Section title={t("field.error")}>
          <div className="bg-dangersoft text-dangerink rounded-lg p-2 text-xs">
            <InlineValue value={data.error} />
          </div>
        </Section>
      )}
      <Section
        title={t("tool.executorOutput")}
        actions={
          output.state === "available" ? (
            <ContentActions
              value={output.value}
              name={`${record.record_id}-output`}
              format={typeof output.value === "string" ? "text" : "json"}
            />
          ) : undefined
        }
      >
        <ValueView field={output} />
      </Section>
      <Section
        title={t("tool.modelOutput")}
        actions={
          model.state === "available" ? (
            <ContentActions
              value={model.value}
              name={`${record.record_id}-model-output`}
              format={typeof model.value === "string" ? "text" : "json"}
            />
          ) : undefined
        }
      >
        {same ? (
          <p className="text-n600 text-xs">{t("tool.modelOutputSame")}</p>
        ) : (
          <ValueView field={model} />
        )}
      </Section>
      {metadata && (
        <Section
          title={t("field.metadata")}
          actions={<ContentActions value={metadata} name={`${record.record_id}-metadata`} format="json" />}
        >
          <FieldList>
            {HIGHLIGHTED_METADATA.filter((key) => key in metadata).map((key) => (
              <Field key={key} label={key}>
                <InlineValue value={metadata[key]} />
              </Field>
            ))}
          </FieldList>
          <JsonTree value={metadata} openDepth={1} />
        </Section>
      )}
      {(artifacts.length > 0 || children.length > 0) && (
        <Section title={t("tool.produced")}>
          <ul className="flex flex-col gap-1">
            {[...children, ...artifacts].map((item) => (
              <li key={item.record_id}>
                <RecordLink recordId={item.record_id} />
              </li>
            ))}
          </ul>
        </Section>
      )}
    </div>
  )
}
