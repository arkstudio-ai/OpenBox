import { lazy, Suspense, useLayoutEffect, useRef, useState } from "react"
import { ChevronDown, FileText } from "lucide-react"
import { useTranslation } from "react-i18next"
import { cn } from "@/shared/lib/cn"
import type { FilePart, MessageWithParts, TextPart } from "@/shared/types/api"
import { AttachmentGallery } from "./AttachmentGallery"
import { isGalleryMedia } from "../lib/media"
import { isAssistantDelegation } from "../lib/turn-view"
import { voiceRequest } from "../lib/voice-request"
import { SentByAssistantBadge } from "./meta/MetaBadges"
import { UserMeta } from "./meta/UserMeta"
import { SendReceipt } from "./SendReceipt"

const ATTACH_MARK = "\n\n[attachments]\n"

/** Splits the composer's attachment trailer off the visible message text. */
function splitAttachments(full: string): { text: string; files: string[] } {
  const at = full.indexOf(ATTACH_MARK)
  if (at === -1) return { text: full, files: [] }
  const files = full
    .slice(at + ATTACH_MARK.length)
    .split("\n")
    .map((l) => l.replace(/^- /, "").trim())
    .filter(Boolean)
  return { text: full.slice(0, at), files }
}

/** Joins the user message's text parts into one visible string. Synthetic
 *  parts are platform protocol and stay hidden, except an instruction the
 *  personal assistant sent on the user's behalf (see isAssistantDelegation). */
function userMessageText(message: MessageWithParts): { text: string; files: string[] } {
  const full = message.parts
    .filter(
      (p): p is TextPart => p.type === "text" && (!p.synthetic || p.origin === "assistant_delegation"),
    )
    .map((p) => p.text)
    .join("\n")
    .trim()
  return splitAttachments(full)
}

const Markdown = lazy(() => import("./Markdown"))

/** Right-aligned user message bubble + attachment chips below (design 5.6). */
export function UserBubble({ message }: { message: MessageWithParts }) {
  const { t } = useTranslation("chat")
  const { text: original, files } = userMessageText(message)
  const request = voiceRequest(message)
  const text = request ?? original
  // OSS-era messages carry proper file parts (with asset ids for previews);
  // the text trailer is only the fallback for messages sent before that.
  const fileParts = message.parts.filter((p): p is FilePart => p.type === "file")
  const legacyFiles = fileParts.length > 0 ? [] : files
  const bodyRef = useRef<HTMLDivElement>(null)
  const [expanded, setExpanded] = useState(false)
  const [clamped, setClamped] = useState(false)

  useLayoutEffect(() => {
    const el = bodyRef.current
    // scrollHeight reports the full content height even while clamped.
    if (el) setClamped(el.scrollHeight > 128)
  }, [text])

  if (!text && fileParts.length === 0 && files.length === 0) return null
  const showFold = clamped && !expanded

  return (
    <div className="group/msg flex min-w-0 max-w-full flex-col items-end gap-2">
      {text && (
        <div
          ref={bodyRef}
          className={cn(
            "bg-n200/60 text-ink min-w-0 max-w-[70%] overflow-hidden rounded-xl p-3 text-lg leading-8 [overflow-wrap:anywhere] transition-[max-height] duration-200 max-sm:max-w-[88%]",
            showFold && "max-h-32",
          )}
        >
          {request && <p className="text-n600 mb-1 text-xs leading-5">{t("message.voiceRequest")}</p>}
          <Suspense fallback={<span className="whitespace-pre-wrap">{text}</span>}>
            <Markdown text={text} variant="user" />
          </Suspense>
        </div>
      )}
      {clamped && (
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          className="text-n600 hover:text-ink inline-flex items-center gap-0.5 text-xs transition-colors"
        >
          <ChevronDown className={cn("size-3.5 transition-transform", expanded && "rotate-180")} strokeWidth={1.8} />
          {expanded ? t("meta.collapseMessage") : t("meta.expandMessage")}
        </button>
      )}
      {request && <details className="text-n600 max-w-[70%] text-xs max-sm:max-w-[88%]">
        <summary className="cursor-pointer text-end">{t("message.voiceTranscript")}</summary>
        <p className="mt-1 whitespace-pre-wrap text-start leading-relaxed [overflow-wrap:anywhere]">{original}</p>
      </details>}
      <AttachmentGallery className="items-end" parts={fileParts.filter(isGalleryMedia)} />
      {fileParts
        .filter((p) => !isGalleryMedia(p))
        .map((part) => (
          <div key={part.id} className="border-hair flex items-center gap-2 rounded-full border py-1 ps-1.5 pe-3.5">
            <span className="bg-n200 flex size-5.5 items-center justify-center rounded-full">
              <FileText className="text-n600 size-3" />
            </span>
            <span className="text-ink font-mono text-xs">{part.path.split("/").pop()}</span>
          </div>
        ))}
      {legacyFiles.map((path) => (
        <div
          key={path}
          className="border-hair flex items-center gap-2 rounded-full border py-1 ps-1.5 pe-3.5"
        >
          <span className="bg-n200 flex size-5.5 items-center justify-center rounded-full">
            <FileText className="text-n600 size-3" />
          </span>
          <span className="text-ink font-mono text-xs">{path.split("/").pop()}</span>
        </div>
      ))}
      {isAssistantDelegation(message) && <SentByAssistantBadge />}
      <UserMeta content={text} createdAt={message.created_at} />
      <SendReceipt message={message} />
    </div>
  )
}
