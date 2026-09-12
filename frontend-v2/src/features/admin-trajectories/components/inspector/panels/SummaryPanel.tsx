import { useTranslation } from "react-i18next"
import { FIELD_LABELS, LINK_PREFIXES, SUMMARY_FIELDS } from "../../../constants/inspector"
import { labelKey, RELATION_LABELS } from "../../../constants/labels"
import { ID_FIELDS } from "../../../types/protocol"
import { isClosed } from "../../../utils/availability"
import { relationsFor } from "../../../utils/relations"
import { Status } from "../../StatusPills"
import { AvailabilityNote } from "../AvailabilityNote"
import { ContentActions } from "../ContentActions"
import { CopyButton } from "../CopyButton"
import { DurationValue } from "../DurationValue"
import { Field, FieldList, Section } from "../Field"
import { InlineValue } from "../InlineValue"
import { InstantValue } from "../InstantValue"
import { JsonTree } from "../JsonTree"
import { RecordLink } from "../RecordLink"
import { useInspector } from "../context"
import { NS, type PanelProps } from "../types"

const TEXT_BLOCKS = new Set(["text", "output_text"])

function AssistantFacts({ record }: PanelProps) {
  const { t } = useTranslation(NS)
  const tools = record.blocks.filter((block) => block.type === "tool_arguments").length
  const text = record.blocks.some((block) => TEXT_BLOCKS.has(String(block.type)) && block.text)
  const reasoning = record.blocks.some((block) => String(block.type).startsWith("reasoning") && block.text)
  return (
    <>
      <Field label={t("summary.outputShape")}>
        {!record.blocks.length ? (
          <AvailabilityNote state={isClosed(record) ? "empty" : "pending"} />
        ) : (
          t(
            text
              ? "summary.shapeText"
              : tools
                ? "summary.shapeToolsOnly"
                : reasoning
                  ? "summary.shapeReasoningOnly"
                  : "summary.shapeOther",
            {
              tools,
            },
          )
        )}
      </Field>
      {isClosed(record) && record.status !== "completed" && record.blocks.length > 0 && (
        <Field label={t("summary.interrupted")}>{t("summary.prefixKept")}</Field>
      )}
    </>
  )
}

function ToolFacts({ record }: PanelProps) {
  const { t } = useTranslation(NS)
  const { records } = useInspector()
  const all = Object.values(records)
  const children = all.filter(
    (item) => item.kind === "tool" && record.call_id && item.parent_call_id === record.call_id,
  ).length
  const artifacts = all.filter(
    (item) => item.kind === "artifact" && record.call_id && item.call_id === record.call_id,
  ).length
  return (
    <>
      <Field label={t("summary.resultPreview")}>
        {record.result_preview ?? <AvailabilityNote state={isClosed(record) ? "not_recorded" : "pending"} />}
      </Field>
      <Field label={t("summary.childCalls")}>{children}</Field>
      <Field label={t("summary.artifacts")}>{artifacts}</Field>
      {!record.started_at && isClosed(record) && (
        <Field label={t("summary.execution")}>{t("summary.notExecuted")}</Field>
      )}
    </>
  )
}

function AdvancedIds({ record }: PanelProps) {
  const { t } = useTranslation(NS)
  const ids: Array<[string, string | number | null]> = [
    ["record_id", record.record_id],
    ...ID_FIELDS.map((key): [string, string | number | null] => [key, record[key]]),
    ["start_seq", record.start_seq],
    ["end_seq", record.end_seq],
    ["as_of_seq", record.as_of_seq],
  ]
  return (
    <details className="border-hair rounded-lg border px-3 py-2">
      <summary className="text-n700 cursor-pointer text-xs font-medium">{t("summary.advanced")}</summary>
      <dl className="mt-2 grid grid-cols-[max-content_minmax(0,1fr)] gap-x-4 gap-y-1 text-xs">
        {ids
          .filter(([, value]) => value !== null && value !== undefined)
          .map(([key, value]) => (
            <div key={key} className="contents">
              <dt className="text-n500 font-mono">{key}</dt>
              <dd className="text-ink flex min-w-0 items-center gap-1 font-mono break-all">
                <span className="min-w-0">{String(value)}</span>
                <CopyButton text={String(value)} label={t("common.copyValue")} className="size-5" />
              </dd>
            </div>
          ))}
      </dl>
    </details>
  )
}

/** Overview of any record: lifecycle, the kind's identifying facts, relations and every captured field. */
export function SummaryPanel({ record }: PanelProps) {
  const { t } = useTranslation(NS)
  const { records } = useInspector()
  const facts = (SUMMARY_FIELDS[record.kind] ?? []).filter((key) =>
    Object.prototype.hasOwnProperty.call(record.data ?? {}, key),
  )
  const relations = relationsFor(record, records)
  return (
    <div className="flex flex-col gap-4" data-testid="trajectory-summary">
      <FieldList>
        <Field label={t("summary.status")}>
          <span className="inline-flex flex-wrap items-center gap-2">
            <Status scope="record" value={record.status} reason={record.status_reason} />
            {record.status_reason && <span className="text-n600">{record.status_reason}</span>}
          </span>
        </Field>
        <Field label={t("summary.title")}>{record.title}</Field>
        {record.preview && <Field label={t("summary.preview")}>{record.preview}</Field>}
        {record.kind === "assistant" && <AssistantFacts record={record} />}
        {record.kind === "tool" && <ToolFacts record={record} />}
        <Field label={t("summary.started")}>
          <InstantValue iso={record.started_at} />
        </Field>
        {record.finished_at && (
          <Field label={t("summary.finished")}>
            <InstantValue iso={record.finished_at} />
          </Field>
        )}
        <Field label={t("summary.duration")}>
          {record.duration_ms !== null ? <DurationValue ms={record.duration_ms} /> : t("common.dash")}
        </Field>
        {facts.map((key) => (
          <Field key={key} label={t(labelKey(FIELD_LABELS, key, "field.other"), { value: key })}>
            <InlineValue value={record.data[key]} linkPrefix={LINK_PREFIXES[key]} />
          </Field>
        ))}
      </FieldList>
      {relations.length > 0 && (
        <Section title={t("summary.relations")}>
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
      <AdvancedIds record={record} />
      <Section
        title={t("summary.allFields")}
        actions={<ContentActions value={record.data} name={`${record.record_id}-data`} format="json" />}
      >
        <JsonTree value={record.data} openDepth={0} />
      </Section>
    </div>
  )
}
