import { useTranslation } from "react-i18next"
import { BLOCK_TYPE_LABELS, labelKey } from "../../../constants/labels"
import { isPlainObject } from "../../../utils/python"
import { AvailabilityNote } from "../AvailabilityNote"
import { ContentActions } from "../ContentActions"
import { Section } from "../Field"
import { JsonTree } from "../JsonTree"
import { TextBlock } from "../TextBlock"
import { NS, type PanelProps } from "../types"
import { hasSanitizedRaw } from "./eventNotes"

/** Captured structure without rendering: ordered blocks with their ids and chunk indexes, and the stored fields. */
export function RawContentPanel({ record }: PanelProps) {
  const { t } = useTranslation(NS)
  const parts = record.data?.committed_parts
  return (
    <div className="flex flex-col gap-4">
      {hasSanitizedRaw(record.events) && (
        <p className="bg-hairsoft text-n700 rounded-lg px-3 py-2 text-xs">{t("events.sanitizedRawNote")}</p>
      )}
      <Section
        title={t("raw.blocks", { count: record.blocks.length })}
        actions={
          record.blocks.length ? (
            <ContentActions value={record.blocks} name={`${record.record_id}-blocks`} format="json" />
          ) : undefined
        }
      >
        {!record.blocks.length && <AvailabilityNote state="not_recorded" />}
        <ol className="flex flex-col gap-2">
          {record.blocks.map((block, index) => (
            <li key={block.block_id} className="border-hair rounded-lg border p-2">
              <p className="text-n600 text-2xs mb-1 flex flex-wrap gap-x-3 font-mono">
                <span>#{index}</span>
                <span>
                  {t(labelKey(BLOCK_TYPE_LABELS, String(block.type), "block.other"), {
                    value: String(block.type),
                  })}
                </span>
                <span>{block.block_id}</span>
                {block.chunk_index !== undefined && (
                  <span>{t("raw.chunk", { index: block.chunk_index })}</span>
                )}
              </p>
              {block.text ? <TextBlock text={block.text} /> : <AvailabilityNote state="empty" />}
            </li>
          ))}
        </ol>
      </Section>
      {isPlainObject(parts) && (
        <Section
          title={t("raw.committedParts")}
          actions={<ContentActions value={parts} name={`${record.record_id}-parts`} format="json" />}
        >
          <JsonTree value={parts} />
        </Section>
      )}
      <Section
        title={t("raw.data")}
        actions={<ContentActions value={record.data} name={`${record.record_id}-data`} format="json" />}
      >
        <JsonTree value={record.data} />
      </Section>
    </div>
  )
}
