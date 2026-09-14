import { useTranslation } from "react-i18next"
import { isPlainObject } from "../../utils/python"
import { AvailabilityNote } from "./AvailabilityNote"
import { JsonTree } from "./JsonTree"
import { findEnvelopes } from "./media"
import { MediaRefView } from "./MediaRefView"
import { TextBlock } from "./TextBlock"
import { NS } from "./types"

interface MessageViewProps {
  message: unknown
  index: number
}

interface ContentPartProps {
  part: unknown
}

const TEXT_PART = new Set(["text", "input_text", "output_text"])
const MEDIA_PART = /image|file|audio|video|document/

function ContentPart({ part }: ContentPartProps) {
  const { t } = useTranslation(NS)
  if (typeof part === "string") return <TextBlock text={part} />
  if (isPlainObject(part) && TEXT_PART.has(String(part.type)) && typeof part.text === "string")
    return <TextBlock text={part.text} />
  const retained = findEnvelopes(part)
  if (retained.length) {
    // Media the model actually received, retained at capture time: shown through the protected endpoint.
    return (
      <div className="flex flex-col gap-1.5" data-testid="trajectory-message-media">
        {retained.map((envelope, index) => (
          <MediaRefView key={index} value={envelope} autoLoad />
        ))}
        <JsonTree value={part} openDepth={0} />
      </div>
    )
  }
  if (isPlainObject(part) && MEDIA_PART.test(String(part.type))) {
    // A reference without retained content (e.g. a remote URL) is shown as data and never fetched.
    return (
      <div className="flex flex-col gap-1">
        <p className="text-n600 text-2xs">{t("request.mediaReference", { type: String(part.type) })}</p>
        <JsonTree value={part} openDepth={1} />
      </div>
    )
  }
  return <JsonTree value={part} openDepth={1} />
}

/** One model-facing message in its original shape: role, ordered content parts and any tool calls. */
export function MessageView({ message, index }: MessageViewProps) {
  const { t } = useTranslation(NS)
  if (!isPlainObject(message)) return <JsonTree value={message} />
  const role =
    typeof message.role === "string" ? message.role : typeof message.type === "string" ? message.type : null
  const { content } = message
  const rest = Object.fromEntries(
    Object.entries(message).filter(([key]) => key !== "role" && key !== "content"),
  )
  return (
    <div
      className="border-hair flex flex-col gap-1.5 rounded-lg border p-2"
      data-message-role={role ?? ""}
      data-testid="trajectory-message"
    >
      <p className="text-n600 text-2xs flex gap-2 font-mono">
        <span>#{index + 1}</span>
        <span className="text-ink font-medium">{role ?? t("request.noRole")}</span>
      </p>
      {content === undefined ? null : content === null || content === "" ? (
        <AvailabilityNote state="empty" />
      ) : Array.isArray(content) ? (
        content.map((part, partIndex) => <ContentPart key={partIndex} part={part} />)
      ) : (
        <ContentPart part={content} />
      )}
      {Object.keys(rest).length > 0 && <JsonTree value={rest} openDepth={1} />}
    </div>
  )
}
