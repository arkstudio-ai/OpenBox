import { useTranslation } from "react-i18next"
import { TAB_FIELDS } from "../../../constants/inspector"
import { stableText } from "../../../utils/diff"
import { isPlainObject } from "../../../utils/python"
import { AvailabilityNote } from "../AvailabilityNote"
import { DiffView } from "../DiffView"
import { Section } from "../Field"
import { InlineValue } from "../InlineValue"
import { JsonTree } from "../JsonTree"
import { RecordLink } from "../RecordLink"
import { NS, type PanelProps } from "../types"
import { CapturedFieldsPanel } from "./CapturedFieldsPanel"

const NOTES: Readonly<Record<string, string>> = {
  baseline: "state.baselineNote",
  gap: "state.gapNote",
  late_result: "state.lateNote",
  takeover: "state.takeoverNote",
}

interface TodoListProps {
  value: unknown
}

function TodoList({ value }: TodoListProps) {
  const { t } = useTranslation(NS)
  const items =
    isPlainObject(value) && Array.isArray(value.items) ? value.items : Array.isArray(value) ? value : null
  if (!items) return <AvailabilityNote state="not_recorded" />
  if (!items.length) return <AvailabilityNote state="empty" />
  return (
    <ol className="flex flex-col gap-1">
      {items.map((item, index) => (
        <li key={index} className="flex items-start gap-2 text-xs">
          <span className="text-n500 w-5 flex-none font-mono">{index + 1}</span>
          {isPlainObject(item) ? (
            <span className="min-w-0">
              <span className="text-ink">
                {String(item.content ?? item.title ?? item.text ?? JSON.stringify(item))}
              </span>
              {typeof item.status === "string" && (
                <span className="text-n500 ms-2">{t("state.itemStatus", { status: item.status })}</span>
              )}
            </span>
          ) : (
            <InlineValue value={item} />
          )}
        </li>
      ))}
    </ol>
  )
}

/** State changes and boundaries: the structured value, before/after comparison and what it means for the recording. */
export function StateContentPanel({ record }: PanelProps) {
  const { t } = useTranslation(NS)
  const data = record.data ?? {}
  const hasBefore = "before" in data
  const hasAfter = "after" in data
  const fields = TAB_FIELDS[`${record.kind}.content`]
  const note = NOTES[record.kind]
  const originalRun = typeof data.original_run_id === "string" ? data.original_run_id : null
  return (
    <div className="flex flex-col gap-4" data-testid="trajectory-state-content">
      {note && <p className="bg-hairsoft text-n700 rounded-lg px-3 py-2 text-xs">{t(note)}</p>}
      {originalRun && (
        <RecordLink recordId={`run:${originalRun}`} label={t("state.originalRun", { id: originalRun })} />
      )}
      {record.kind === "todo" && (
        <Section title={t("state.todoItems")}>
          <TodoList value={hasAfter ? data.after : data.items} />
        </Section>
      )}
      {(hasBefore || hasAfter) && (
        <>
          <Section title={t("field.before")}>
            {hasBefore ? <InlineValue value={data.before} /> : <AvailabilityNote state="not_recorded" />}
          </Section>
          <Section title={t("field.after")}>
            {hasAfter ? <InlineValue value={data.after} /> : <AvailabilityNote state="not_recorded" />}
          </Section>
        </>
      )}
      {hasBefore && hasAfter && data.before !== null && (
        <Section title={t("state.changes")}>
          <DiffView before={stableText(data.before)} after={stableText(data.after)} />
        </Section>
      )}
      {fields ? (
        <CapturedFieldsPanel record={record} fields={fields} />
      ) : (
        !hasBefore && !hasAfter && <JsonTree value={data} />
      )}
    </div>
  )
}
