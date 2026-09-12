import { useTranslation } from "react-i18next"
import { formatNumber } from "@/shared/lib/format"
import { FIELD_LABELS, RETRY_FACT_KEYS } from "../../../constants/inspector"
import { labelKey } from "../../../constants/labels"
import { stableText } from "../../../utils/diff"
import { compareRecords, type ViewRecord } from "../../../utils/view"
import { Status } from "../../StatusPills"
import { AvailabilityNote } from "../AvailabilityNote"
import { Field, FieldList, Section } from "../Field"
import { InlineValue } from "../InlineValue"
import { RecordLink } from "../RecordLink"
import { useInspector } from "../context"
import { NS, type PanelProps } from "../types"

function tokenPair(record: ViewRecord): string | null {
  const input = record.usage?.input_tokens
  const output = record.usage?.output_tokens
  return typeof input === "number" && typeof output === "number"
    ? `${formatNumber(input)} / ${formatNumber(output)}`
    : null
}

/**
 * Every attempt of the same step side by side. A retry is a new request: the
 * failed one keeps its own error, input, options and usage.
 */
export function RetryAttemptsPanel({ record }: PanelProps) {
  const { t } = useTranslation(NS)
  const { records } = useInspector()
  const data = record.data ?? {}
  const named = [record.request_id, typeof data.next_request_id === "string" ? data.next_request_id : null]
    .filter((id): id is string => !!id)
    .map((id) => records[`request:${id}`])
    .filter((item): item is ViewRecord => !!item)
  const sameStep = record.step_id
    ? Object.values(records).filter((item) => item.kind === "request" && item.step_id === record.step_id)
    : []
  const attempts = [...new Map([...sameStep, ...named].map((item) => [item.record_id, item])).values()].sort(
    compareRecords,
  )
  return (
    <div className="flex flex-col gap-4" data-testid="trajectory-retry-attempts">
      <FieldList>
        {RETRY_FACT_KEYS.filter((key) => key in data).map((key) => (
          <Field key={key} label={t(labelKey(FIELD_LABELS, key, "field.other"), { value: key })}>
            <InlineValue value={data[key]} />
          </Field>
        ))}
      </FieldList>
      <Section title={t("retry.attempts", { count: attempts.length })}>
        {!attempts.length && <AvailabilityNote state="not_recorded" />}
        <ol className="flex flex-col gap-2">
          {attempts.map((attempt, index) => {
            const previous = attempts[index - 1]
            const changed = previous
              ? stableText(previous.data?.input) !== stableText(attempt.data?.input)
              : null
            const usage = tokenPair(attempt)
            return (
              <li
                key={attempt.record_id}
                className="border-hair flex flex-col gap-1 rounded-lg border p-2 text-xs"
              >
                <span className="flex flex-wrap items-center gap-2">
                  <span className="text-n500">{t("retry.attemptNumber", { index: index + 1 })}</span>
                  <RecordLink recordId={attempt.record_id} />
                  <Status scope="record" value={attempt.status} reason={attempt.status_reason} />
                </span>
                {attempt.status_reason && <span className="text-dangerink">{attempt.status_reason}</span>}
                <span className="text-n600">
                  {usage ? t("retry.usage", { value: usage }) : t("retry.usageMissing")}
                </span>
                {changed !== null && (
                  <span className="text-n600">{t(changed ? "retry.inputChanged" : "retry.inputSame")}</span>
                )}
              </li>
            )
          })}
        </ol>
      </Section>
    </div>
  )
}
