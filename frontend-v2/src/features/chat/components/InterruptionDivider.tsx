import { useState } from "react"
import { useTranslation } from "react-i18next"
import { ChevronDown, Square } from "lucide-react"
import { cn } from "@/shared/lib/cn"
import type { MessageWithParts } from "@/shared/types/api"

function markerText(message: MessageWithParts): string {
  return message.parts
    .filter((p): p is Extract<typeof p, { type: "text" }> => p.type === "text")
    .map((p) => p.text)
    .join("\n")
    .trim()
}

/** A rule across the transcript where a turn was cut short.
 *
 *  Without this the marker renders as an empty bubble — UserBubble drops
 *  synthetic text — which reads as the app having lost a message. The detail
 *  stays available because it is what the model was told, and a person
 *  wondering why the assistant changed course deserves the same answer.
 */
export function InterruptionDivider({ message }: { message: MessageWithParts }) {
  const { t } = useTranslation("chat")
  const [open, setOpen] = useState(false)
  const detail = markerText(message)

  return (
    <div className="my-3 w-full">
      <div className="flex items-center gap-3">
        <span className="bg-hair h-px flex-1" />
        <button
          type="button"
          onClick={() => setOpen((o) => !o)}
          className="text-n500 hover:text-n700 flex shrink-0 items-center gap-1.5 text-xs transition-colors"
        >
          <Square className="size-3" strokeWidth={2.5} />
          {t("interrupted.label")}
          {detail && (
            <ChevronDown
              className={cn("size-3 transition-transform duration-200", open && "rotate-180")}
            />
          )}
        </button>
        <span className="bg-hair h-px flex-1" />
      </div>
      {open && detail && (
        <pre className="text-n600 border-hair mt-2 overflow-x-auto rounded-lg border p-3 text-xs whitespace-pre-wrap">
          {detail}
        </pre>
      )}
    </div>
  )
}
