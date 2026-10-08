// "参考了 3 条记忆" under a reply of the personal assistant: the memories that came up for the
// message it answered, so the person sees what it drew on and can fix what is wrong.
import { useEffect, useId, useRef, useState } from "react"
import { ChevronDown, History } from "lucide-react"
import { useTranslation } from "react-i18next"
import { Link } from "react-router"
import { cn } from "@/shared/lib/cn"
import { paths } from "@/shared/router/paths"
import { useRecalledMemories } from "../api/recalled"

export function RecalledMemories({
  sessionId,
  messageId,
  streaming,
}: {
  sessionId: string
  /** The user's message the reply answered. */
  messageId: string | null | undefined
  streaming: boolean
}) {
  const { t } = useTranslation("chat")
  const recalled = useRecalledMemories(sessionId)
  const [open, setOpen] = useState(false)
  const listId = useId()
  // Recall is recorded as the reply starts; read again once it is done, so a new reply shows its own.
  const wasStreaming = useRef(streaming)
  const { refetch } = recalled
  useEffect(() => {
    if (wasStreaming.current && !streaming) void refetch()
    wasStreaming.current = streaming
  }, [streaming, refetch])
  const memories = (messageId && recalled.data?.recalls[messageId]) || []
  if (streaming || memories.length === 0) return null
  return (
    <div className="mt-2">
      <button
        type="button"
        aria-expanded={open}
        aria-controls={listId}
        onClick={() => setOpen((value) => !value)}
        className="bg-n200/40 text-n600 hover:text-n800 inline-flex items-center gap-1.5 rounded px-1.5 py-0.5 text-xs leading-4"
      >
        <History className="size-3 flex-none" strokeWidth={1.6} aria-hidden />
        {t("assistant.recalled.label", { count: memories.length })}
        <ChevronDown
          className={cn("size-3 flex-none transition-transform", open && "rotate-180")}
          aria-hidden
        />
      </button>
      {open && (
        <div id={listId} className="border-hair bg-card mt-1.5 max-w-xl rounded-lg border px-3 py-2.5">
          <p className="text-n700 text-xs font-medium">{t("assistant.recalled.title")}</p>
          <ul className="mt-1.5 flex flex-col gap-1">
            {memories.map((memory) => (
              <li key={memory.id} className="text-ink flex gap-2 text-sm leading-relaxed">
                <span aria-hidden className="text-n500">
                  ·
                </span>
                <span className="min-w-0 break-words">{memory.summary}</span>
              </li>
            ))}
          </ul>
          <p className="text-n600 mt-2 text-xs">
            {t("assistant.recalled.hint")}{" "}
            <Link
              to={paths.wiki(undefined, "memories")}
              className="text-ink underline-offset-2 hover:underline"
            >
              {t("assistant.recalled.manage")}
            </Link>
          </p>
        </div>
      )}
    </div>
  )
}
