import { useTranslation } from "react-i18next"
import { labelKey, TIMING_SOURCE_LABELS } from "../../../constants/labels"
import { isClosed } from "../../../utils/availability"
import { isPlainObject } from "../../../utils/python"
import { POINT_KINDS } from "../../../utils/timeline"
import { elapsedUntil } from "../../../utils/time"
import { AvailabilityNote } from "../AvailabilityNote"
import { DurationValue } from "../DurationValue"
import { Field, FieldList, Section } from "../Field"
import { InstantValue } from "../InstantValue"
import { RecordLink } from "../RecordLink"
import { useInspector } from "../context"
import { NS, type PanelProps } from "../types"

const OUTPUT = ["output_tokens", "completion_tokens", "output"]

function numberField(data: Record<string, unknown>, key: string): number | null {
  return typeof data[key] === "number" ? (data[key] as number) : null
}

function RequestTiming({ record }: PanelProps) {
  const { t, i18n } = useTranslation(NS)
  const data = record.data ?? {}
  const generation = numberField(data, "generation_ms")
  const usage = isPlainObject(record.usage) ? record.usage : {}
  const output =
    OUTPUT.map((key) => usage[key]).find((value): value is number => typeof value === "number") ?? null
  const rate = output !== null && generation !== null && generation > 0 ? output / (generation / 1000) : null
  return (
    <>
      <Field label={t("timing.ttft")}>
        <DurationValue ms={numberField(data, "ttft_ms")} />
      </Field>
      <Field label={t("timing.firstText")}>
        <DurationValue ms={numberField(data, "first_text_ms")} />
      </Field>
      <Field label={t("timing.generation")}>
        <DurationValue ms={generation} />
      </Field>
      <Field label={t("timing.throughput")}>
        {rate === null ? (
          <AvailabilityNote state="not_recorded" />
        ) : (
          t("timing.tokensPerSecond", {
            value: new Intl.NumberFormat(i18n.language, { maximumFractionDigits: 1 }).format(rate),
          })
        )}
      </Field>
    </>
  )
}

function ToolTiming({ record }: PanelProps) {
  const { t } = useTranslation(NS)
  const { records } = useInspector()
  const data = record.data ?? {}
  const permissions = Object.values(records).filter(
    (item) => item.kind === "permission" && record.call_id && item.call_id === record.call_id,
  )
  return (
    <>
      <Field label={t("timing.requested")}>
        <InstantValue iso={typeof data.requested_at === "string" ? data.requested_at : null} />
      </Field>
      <Field label={t("timing.totalWait")}>
        <DurationValue ms={numberField(data, "total_duration_ms")} />
      </Field>
      {permissions.map((permission) => (
        <Field key={permission.record_id} label={t("timing.permissionWait")}>
          <span className="flex flex-wrap items-center gap-2">
            <DurationValue ms={permission.duration_ms} />
            <RecordLink recordId={permission.record_id} />
          </span>
        </Field>
      ))}
      {!record.started_at && isClosed(record) && (
        <Field label={t("timing.execution")}>{t("summary.notExecuted")}</Field>
      )}
    </>
  )
}

/**
 * Recorded instants and durations with where they came from. An interval
 * still open at this position is measured against the position's clock and
 * labelled as an estimate; missing values say so instead of reading 0.
 */
export function TimingPanel({ record }: PanelProps) {
  const { t } = useTranslation(NS)
  const { clock, live } = useInspector()
  const point = POINT_KINDS.has(record.kind)
  const open = !isClosed(record)
  const estimate = open && record.started_at ? elapsedUntil(record.started_at, clock) : null
  const source = record.timing_source
  return (
    <div className="flex flex-col gap-4" data-testid="trajectory-timing">
      <FieldList>
        <Field label={t(point ? "timing.occurred" : "timing.started")}>
          <InstantValue iso={record.started_at} />
        </Field>
        {!point && (
          <Field label={t("timing.finished")}>
            {record.finished_at ? (
              <InstantValue iso={record.finished_at} />
            ) : (
              <AvailabilityNote state={open ? "pending" : "not_recorded"} />
            )}
          </Field>
        )}
        {!point && (
          <Field label={t("timing.duration")}>
            {record.duration_ms !== null ? (
              <DurationValue ms={record.duration_ms} />
            ) : estimate !== null ? (
              <DurationValue ms={estimate} estimate />
            ) : (
              <AvailabilityNote state={open ? "pending" : "not_recorded"} />
            )}
          </Field>
        )}
        {record.kind === "retry" && (
          <Field label={t("field.waitMs")}>
            <DurationValue ms={numberField(record.data ?? {}, "wait_ms")} />
          </Field>
        )}
        {record.kind === "request" && <RequestTiming record={record} />}
        {record.kind === "tool" && <ToolTiming record={record} />}
        <Field label={t("timing.source")}>
          {estimate !== null && record.duration_ms === null
            ? t(live ? "timingSource.liveEstimate" : "timingSource.replayEstimate")
            : source
              ? t(labelKey(TIMING_SOURCE_LABELS, source, "timingSource.other"), { value: source })
              : t("common.dash")}
        </Field>
      </FieldList>
      {estimate !== null && (
        <Section title={t("timing.openTitle")}>
          <p className="text-n600 text-xs">{t(live ? "timing.openLive" : "timing.openReplay")}</p>
        </Section>
      )}
    </div>
  )
}
