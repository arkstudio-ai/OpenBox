import type { FieldState } from "../../utils/availability"
import { AvailabilityNote } from "./AvailabilityNote"
import { JsonTree } from "./JsonTree"
import { MarkdownText } from "./MarkdownText"
import { PayloadView } from "./PayloadView"
import { TextBlock } from "./TextBlock"

export type ValueFormat = "auto" | "text" | "markdown"

interface Props {
  field: FieldState
  format?: ValueFormat
}

/** Renders one captured value with its availability: text, Markdown, JSON or protected content. */
export function ValueView({ field, format = "auto" }: Props) {
  switch (field.state) {
    case "available": {
      const value = field.value
      if (typeof value === "string")
        return format === "markdown" ? <MarkdownText text={value} /> : <TextBlock text={value} />
      if (typeof value === "number" || typeof value === "boolean")
        return <span className="text-ink font-mono text-xs">{String(value)}</span>
      return <JsonTree value={value} />
    }
    case "payload":
      return <PayloadView reference={field.ref} />
    case "deleted":
      return <AvailabilityNote state="deleted" reason={field.reason} />
    default:
      return <AvailabilityNote state={field.state} />
  }
}
