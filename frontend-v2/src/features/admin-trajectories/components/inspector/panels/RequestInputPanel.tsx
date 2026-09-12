import { useTranslation } from "react-i18next"
import { REQUEST_CONTENT_KEYS, REQUEST_OPTION_KEYS } from "../../../constants/inspector"
import { CAPTURE_LEVEL_LABELS, labelKey } from "../../../constants/labels"
import { fieldState } from "../../../utils/availability"
import { isPlainObject, type PlainObject } from "../../../utils/python"
import { AvailabilityNote } from "../AvailabilityNote"
import { ContentActions } from "../ContentActions"
import { Section } from "../Field"
import { JsonTree } from "../JsonTree"
import { isContentEnvelope } from "../media"
import { MediaRefView } from "../MediaRefView"
import { MessageView } from "../MessageView"
import { TextBlock } from "../TextBlock"
import { ToolDefinitionList } from "../ToolDefinitionList"
import { NS, type PanelProps } from "../types"
import { ValueView } from "../ValueView"

interface SnapshotProps {
  input: PlainObject
  name: string
}

interface MessagesProps {
  value: unknown
}

interface MediaInputsProps {
  value: unknown
}

const CAPTURE_NOTES: Readonly<Record<string, string>> = {
  adapter_input: "request.captureAdapter",
  provider_wire: "request.captureWire",
}

/** Keys rendered by a dedicated section; everything else appears under "other captured fields". */
const HANDLED = new Set<string>([
  ...REQUEST_CONTENT_KEYS,
  ...REQUEST_OPTION_KEYS,
  "omitted_fields",
  "extra_body",
  "media_inputs",
])

function Messages({ value }: MessagesProps) {
  if (typeof value === "string") return <TextBlock text={value} />
  if (!Array.isArray(value)) return <ValueView field={{ state: "available", value }} />
  return (
    <div className="flex flex-col gap-2">
      {value.map((message, index) => (
        <MessageView key={index} message={message} index={index} />
      ))}
    </div>
  )
}

function MediaInputs({ value }: MediaInputsProps) {
  const items = Array.isArray(value) ? value : [value]
  return (
    <div className="flex flex-col gap-2" data-testid="trajectory-media-inputs">
      {items.map((item, index) =>
        isContentEnvelope(item) ? (
          <MediaRefView key={index} value={item} autoLoad />
        ) : (
          <JsonTree key={index} value={item} openDepth={1} />
        ),
      )}
    </div>
  )
}

function Snapshot({ input, name }: SnapshotProps) {
  const { t } = useTranslation(NS)
  const system = input.system ?? input.instructions
  const messages = input.messages ?? input.input
  const tools = Array.isArray(input.tools) ? input.tools : null
  const omitted = Array.isArray(input.omitted_fields) ? input.omitted_fields.map(String) : []
  const extra = isPlainObject(input.extra_body) ? input.extra_body : null
  const other = Object.entries(input).filter(([key]) => !HANDLED.has(key))
  return (
    <>
      {system !== undefined && (
        <Section
          title={t("request.system")}
          actions={
            <ContentActions
              value={system}
              name={`${name}-system`}
              format={typeof system === "string" ? "text" : "json"}
            />
          }
        >
          <Messages value={system} />
        </Section>
      )}
      <Section
        title={t("request.messages")}
        actions={
          messages !== undefined ? (
            <ContentActions value={messages} name={`${name}-messages`} format="json" />
          ) : undefined
        }
      >
        {messages === undefined ? <AvailabilityNote state="not_recorded" /> : <Messages value={messages} />}
      </Section>
      {typeof input.prompt === "string" && (
        <Section title={t("request.prompt")}>
          <TextBlock text={input.prompt} />
        </Section>
      )}
      {input.media_inputs !== undefined && (
        <Section title={t("request.mediaInputs")}>
          <MediaInputs value={input.media_inputs} />
        </Section>
      )}
      <Section title={t("request.tools", { count: tools?.length ?? 0 })}>
        {tools === null ? (
          <AvailabilityNote state={"tools" in input ? "empty" : "not_recorded"} />
        ) : (
          <ToolDefinitionList tools={tools} />
        )}
      </Section>
      {extra && (
        <Section title={t("request.extraBody")}>
          <JsonTree value={extra} openDepth={1} />
        </Section>
      )}
      {other.length > 0 && (
        <Section title={t("request.otherFields")}>
          <JsonTree value={Object.fromEntries(other)} openDepth={1} />
        </Section>
      )}
      {omitted.length > 0 && (
        <p className="text-n600 text-xs" data-testid="trajectory-omitted-fields">
          {t("request.omitted", { fields: omitted.join(", ") })}
        </p>
      )}
      <details className="border-hair rounded-lg border px-3 py-2" data-testid="trajectory-request-raw">
        <summary className="text-n700 cursor-pointer text-xs font-medium">{t("request.rawSnapshot")}</summary>
        <div className="mt-2">
          <JsonTree value={input} openDepth={1} />
        </div>
      </details>
    </>
  )
}

/**
 * The request as the adapter actually dispatched it — effective system text,
 * ordered messages with the media the model received, visible tools, every
 * other captured field and the complete raw snapshot — at its declared capture
 * level. Distinct from the user's own words and from a tool's arguments.
 */
export function RequestInputPanel({ record }: PanelProps) {
  const { t } = useTranslation(NS)
  const capture = typeof record.data?.capture_level === "string" ? record.data.capture_level : null
  const input = fieldState(record, "input")
  const serviceMedia = record.data?.media_inputs
  return (
    <div className="flex flex-col gap-4" data-testid="trajectory-request-input">
      <div className="bg-hairsoft flex flex-wrap items-center gap-2 rounded-lg px-3 py-2 text-xs">
        <span className="text-ink font-medium">
          {capture
            ? t(labelKey(CAPTURE_LEVEL_LABELS, capture, "captureLevel.other"), { value: capture })
            : t("request.captureUnknown")}
        </span>
        {capture && CAPTURE_NOTES[capture] && <span className="text-n600">{t(CAPTURE_NOTES[capture])}</span>}
        {input.state === "available" && (
          <ContentActions value={input.value} name={`${record.record_id}-input`} format="json" />
        )}
      </div>
      {input.state === "available" && isPlainObject(input.value) ? (
        <Snapshot input={input.value} name={record.record_id} />
      ) : (
        <ValueView field={input} />
      )}
      {serviceMedia !== undefined && (
        <Section title={t("request.mediaInputs")}>
          <MediaInputs value={serviceMedia} />
        </Section>
      )}
    </div>
  )
}
