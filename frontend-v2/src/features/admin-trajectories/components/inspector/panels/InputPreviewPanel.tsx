import { useTranslation } from "react-i18next"
import { isPlainObject } from "../../../utils/python"
import { firstField } from "../../../utils/availability"
import { AvailabilityNote } from "../AvailabilityNote"
import { Field, FieldList, Section } from "../Field"
import { InlineValue } from "../InlineValue"
import { MarkdownText } from "../MarkdownText"
import { RecordLink } from "../RecordLink"
import { useInspector } from "../context"
import { NS, type PanelProps } from "../types"
import { ValueView } from "../ValueView"

const STATUS_NOTES: Readonly<Record<string, string>> = {
  pending: "input.statusPending",
  accepted: "input.statusAccepted",
  injected: "input.statusInjected",
  cancelled: "input.statusCancelled",
}

function committedTexts(value: unknown): Array<{ id: string; text: string }> {
  if (!isPlainObject(value)) return []
  return Object.entries(value).flatMap(([id, part]) =>
    isPlainObject(part) && typeof part.text === "string" && part.text ? [{ id, text: part.text }] : [],
  )
}

/** What the person (or an injection source) submitted: text, attachments and where it entered the context. */
export function InputPreviewPanel({ record }: PanelProps) {
  const { t } = useTranslation(NS)
  const { records } = useInspector()
  const content = firstField(record, ["text", "content"])
  const attachments = Array.isArray(record.data?.attachments) ? record.data.attachments : null
  const linked = Object.values(records).filter(
    (item) => item.kind === "artifact" && record.message_id && item.message_id === record.message_id,
  )
  const saved = committedTexts(record.data?.committed_parts)
  const note = STATUS_NOTES[record.status ?? ""]
  return (
    <div className="flex flex-col gap-4">
      {note && <p className="bg-hairsoft text-n700 rounded-lg px-3 py-2 text-xs">{t(note)}</p>}
      <Section title={t("input.content")}>
        <ValueView field={content} format="markdown" />
      </Section>
      {saved.length > 0 && (
        <Section title={t("input.savedParts")}>
          {saved.map((part) => (
            <MarkdownText key={part.id} text={part.text} />
          ))}
        </Section>
      )}
      <Section title={t("input.attachments")}>
        {attachments === null && !linked.length && <AvailabilityNote state="not_recorded" />}
        {attachments?.length === 0 && <AvailabilityNote state="empty" />}
        {attachments?.map((attachment, index) => (
          <div key={index} className="border-hair rounded-lg border p-2">
            {isPlainObject(attachment) ? (
              <FieldList>
                {Object.entries(attachment).map(([key, value]) => (
                  <Field key={key} label={key}>
                    <InlineValue value={value} />
                  </Field>
                ))}
              </FieldList>
            ) : (
              <InlineValue value={attachment} />
            )}
          </div>
        ))}
        {linked.map((item) => (
          <RecordLink key={item.record_id} recordId={item.record_id} />
        ))}
      </Section>
    </div>
  )
}
