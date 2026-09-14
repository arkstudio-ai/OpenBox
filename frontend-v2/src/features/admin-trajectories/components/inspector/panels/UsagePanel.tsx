import { useTranslation } from "react-i18next"
import { formatNumber } from "@/shared/lib/format"
import { USAGE_LABELS } from "../../../constants/inspector"
import { labelKey } from "../../../constants/labels"
import { isClosed } from "../../../utils/availability"
import { isPlainObject } from "../../../utils/python"
import { agentScope, type ViewRecord } from "../../../utils/view"
import { AvailabilityNote } from "../AvailabilityNote"
import { Field, FieldList, Section } from "../Field"
import { InlineValue } from "../InlineValue"
import { RecordLink } from "../RecordLink"
import { useInspector } from "../context"
import { NS, type InspectedRecord, type PanelProps } from "../types"

interface UsageListProps {
  usage: Record<string, unknown>
}

interface TotalsProps {
  requests: readonly ViewRecord[]
}

const INPUT = ["input_tokens", "prompt_tokens", "input"]
const OUTPUT = ["output_tokens", "completion_tokens", "output"]

function tokens(usage: unknown, names: readonly string[]): number | null {
  if (!isPlainObject(usage)) return null
  for (const name of names) if (typeof usage[name] === "number") return usage[name] as number
  return null
}

function UsageList({ usage }: UsageListProps) {
  const { t } = useTranslation(NS)
  const known = Object.keys(USAGE_LABELS).filter((key) => key in usage)
  const other = Object.keys(usage).filter((key) => !(key in USAGE_LABELS))
  return (
    <FieldList>
      {[...known, ...other].map((key) => (
        <Field key={key} label={t(labelKey(USAGE_LABELS, key, "field.other"), { value: key })}>
          {typeof usage[key] === "number" ? (
            <span className="font-mono">{formatNumber(usage[key] as number)}</span>
          ) : (
            <InlineValue value={usage[key]} />
          )}
        </Field>
      ))}
    </FieldList>
  )
}

/** Sums over request records, each counted once; missing usage is reported, never taken as zero. */
function Totals({ requests }: TotalsProps) {
  const { t } = useTranslation(NS)
  let input = 0
  let output = 0
  let missing = 0
  for (const request of requests) {
    const inTokens = tokens(request.usage, INPUT)
    const outTokens = tokens(request.usage, OUTPUT)
    if (inTokens === null || outTokens === null) missing += 1
    input += inTokens ?? 0
    output += outTokens ?? 0
  }
  return (
    <FieldList>
      <Field label={t("usage.requests")}>{formatNumber(requests.length)}</Field>
      <Field label={t("usage.inputTokens")}>
        {requests.length > missing ? formatNumber(input) : <AvailabilityNote state="not_recorded" />}
      </Field>
      <Field label={t("usage.outputTokens")}>
        {requests.length > missing ? formatNumber(output) : <AvailabilityNote state="not_recorded" />}
      </Field>
      {missing > 0 && (
        <Field label={t("usage.incomplete")}>{t("usage.missingCount", { count: missing })}</Field>
      )}
    </FieldList>
  )
}

function OwnUsage({ record }: { record: InspectedRecord }) {
  const { t } = useTranslation(NS)
  if (Object.keys(record.usage ?? {}).length === 0)
    return <AvailabilityNote state={isClosed(record) ? "not_recorded" : "pending"} />
  return (
    <>
      <UsageList usage={record.usage} />
      {"credits" in record.usage && <p className="text-n600 text-2xs">{t("usage.ledgerNote")}</p>}
    </>
  )
}

/** Request usage, the session total at this position, or an agent's direct and descendant totals. */
export function UsagePanel({ record }: PanelProps) {
  const { t } = useTranslation(NS)
  const { records, statistics, throughSeq } = useInspector()
  const all = Object.values(records)
  if (record.kind === "agent") {
    const scope = record.agent_id ? agentScope(all, record.agent_id) : new Set<string>()
    const requests = all.filter(
      (item) => item.kind === "request" && item.agent_id && scope.has(item.agent_id),
    )
    return (
      <div className="flex flex-col gap-4">
        <Section title={t("usage.direct")}>
          <Totals requests={requests.filter((item) => item.agent_id === record.agent_id)} />
        </Section>
        <Section title={t("usage.withDescendants")}>
          <Totals requests={requests} />
        </Section>
      </div>
    )
  }
  const requestId =
    record.kind === "request"
      ? record.request_id
      : typeof record.data?.request_id === "string"
        ? record.data.request_id
        : null
  const request = requestId ? records[`request:${requestId}`] : undefined
  return (
    <div className="flex flex-col gap-4" data-testid="trajectory-usage">
      <Section title={t("usage.thisRequest")}>
        {record.kind === "request" && <OwnUsage record={record} />}
        {record.kind !== "request" && requestId && (
          <RecordLink recordId={`request:${requestId}`} label={requestId} />
        )}
        {record.kind !== "request" && request && <UsageList usage={request.usage} />}
        {record.kind !== "request" && !requestId && <AvailabilityNote state="not_recorded" />}
      </Section>
      <Section title={t("usage.session", { seq: throughSeq })}>
        <FieldList>
          <Field label={t("usage.requests")}>{formatNumber(statistics.request_count)}</Field>
          <Field label={t("usage.inputTokens")}>
            {statistics.input_tokens === null ? (
              <AvailabilityNote state="not_recorded" />
            ) : (
              formatNumber(statistics.input_tokens)
            )}
          </Field>
          <Field label={t("usage.outputTokens")}>
            {statistics.output_tokens === null ? (
              <AvailabilityNote state="not_recorded" />
            ) : (
              formatNumber(statistics.output_tokens)
            )}
          </Field>
          <Field label={t("usage.completeness")}>
            {t(statistics.usage_complete ? "usage.complete" : "usage.partial")}
          </Field>
        </FieldList>
      </Section>
    </div>
  )
}
