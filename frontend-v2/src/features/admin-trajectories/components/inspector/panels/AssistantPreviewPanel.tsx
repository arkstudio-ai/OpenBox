import { useTranslation } from "react-i18next"
import { Brain, Wrench } from "lucide-react"
import { BLOCK_TYPE_LABELS, labelKey } from "../../../constants/labels"
import type { RecordBlock } from "../../../types/protocol"
import { isClosed } from "../../../utils/availability"
import { isPlainObject } from "../../../utils/python"
import { AvailabilityNote } from "../AvailabilityNote"
import { Section } from "../Field"
import { MarkdownText } from "../MarkdownText"
import { RecordLink } from "../RecordLink"
import { TextBlock } from "../TextBlock"
import { useInspector } from "../context"
import { NS, type PanelProps } from "../types"

interface BlockViewProps {
  block: RecordBlock
  open: boolean
}

function parsesAsJson(text: string): boolean {
  try {
    JSON.parse(text)
    return true
  } catch {
    return false
  }
}

function BlockView({ block, open }: BlockViewProps) {
  const { t } = useTranslation(NS)
  const type = String(block.type)
  const label = t(labelKey(BLOCK_TYPE_LABELS, type, "block.other"), { value: type })
  if (type.startsWith("reasoning")) {
    return (
      <details className="border-hair bg-surface rounded-lg border px-3 py-2" data-block-type="reasoning">
        <summary className="text-n700 flex cursor-pointer items-center gap-2 text-xs">
          <Brain size={13} aria-hidden />
          {t("assistant.reasoning", { count: block.text.length })}
        </summary>
        <div className="mt-2">
          <TextBlock text={block.text} mono={false} />
        </div>
      </details>
    )
  }
  if (type === "text" || type === "output_text") {
    return (
      <div data-block-type="text">
        {block.text ? (
          <MarkdownText text={block.text} />
        ) : (
          <AvailabilityNote state={open ? "pending" : "empty"} />
        )}
      </div>
    )
  }
  if (type === "tool_arguments") {
    const generating = open && !parsesAsJson(block.text)
    return (
      <div className="border-hair rounded-lg border p-2" data-block-type="tool_arguments">
        <p className="text-n700 mb-1 flex items-center gap-2 text-xs">
          <Wrench size={13} aria-hidden />
          <span className="font-mono">{typeof block.tool === "string" ? block.tool : block.block_id}</span>
          {generating && <span className="text-a700">{t("tool.argumentsGenerating")}</span>}
        </p>
        {block.text ? (
          <TextBlock text={block.text} />
        ) : (
          <AvailabilityNote state={open ? "pending" : "empty"} />
        )}
      </div>
    )
  }
  return (
    <Section title={label}>
      <TextBlock text={block.text} />
    </Section>
  )
}

/**
 * The AI output exactly as captured: returned reasoning, text and tool-call
 * argument blocks in their original order and kind. Nothing is generated for
 * a block the provider did not return.
 */
export function AssistantPreviewPanel({ record }: PanelProps) {
  const { t } = useTranslation(NS)
  const { records } = useInspector()
  const open = !isClosed(record)
  const calls = Object.values(records).filter(
    (item) => item.kind === "tool" && record.request_id && item.request_id === record.request_id,
  )
  const committed = isPlainObject(record.data?.committed_parts)
    ? Object.values(record.data.committed_parts)
    : []
  const onlyTools =
    record.blocks.length > 0 && record.blocks.every((block) => block.type === "tool_arguments")
  return (
    <div className="flex flex-col gap-3" data-testid="trajectory-assistant-preview">
      {typeof record.data?.channel === "string" && (
        <p className="text-n600 text-xs">
          {t(record.data.channel === "commentary" ? "assistant.commentary" : "assistant.final")}
        </p>
      )}
      {onlyTools && (
        <p className="bg-hairsoft text-n700 rounded-lg px-3 py-2 text-xs">{t("assistant.onlyTools")}</p>
      )}
      {!open && record.status !== "completed" && record.blocks.length > 0 && (
        <p className="bg-a100 text-a700 rounded-lg px-3 py-2 text-xs">{t("assistant.prefixKept")}</p>
      )}
      {record.blocks.length === 0 && committed.length === 0 && (
        <AvailabilityNote state={open ? "pending" : "empty"} />
      )}
      {record.blocks.map((block) => (
        <BlockView key={block.block_id} block={block} open={open} />
      ))}
      {record.blocks.length === 0 &&
        committed.map((part, index) =>
          isPlainObject(part) && typeof part.text === "string" ? (
            <MarkdownText key={index} text={part.text} />
          ) : null,
        )}
      {calls.length > 0 && (
        <Section title={t("assistant.toolCalls", { count: calls.length })}>
          <ul className="flex flex-col gap-1">
            {calls.map((call) => (
              <li key={call.record_id}>
                <RecordLink recordId={call.record_id} />
              </li>
            ))}
          </ul>
        </Section>
      )}
    </div>
  )
}
