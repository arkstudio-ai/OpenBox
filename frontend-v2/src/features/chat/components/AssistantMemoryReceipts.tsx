// What the assistant remembered, updated or forgot in this turn, as compact
// chips under its answer. A fresh memory can be undone right here; the raw
// tool calls stay in the collapsed process trace above.
import { useState } from "react"
import { BookmarkCheck, BookmarkX } from "lucide-react"
import { useTranslation } from "react-i18next"
import { ApiError } from "@/shared/api/http"
import { memoryApi } from "@/shared/api/memory"
import { useApiErrorMessage } from "@/shared/hooks/useApiErrorMessage"
import { cn } from "@/shared/lib/cn"
import { toast } from "@/shared/ui/Toast"
import type { MessagePart, ToolPart } from "@/shared/types/api"
import { memoryReceipt, type MemoryReceipt } from "../lib/memory-receipt"

const CHIP = "inline-flex max-w-full items-center gap-1.5 rounded bg-n200/40 px-1.5 py-0.5 text-xs leading-4 text-n600"
const ICON = "size-3 flex-none"

type Remembered = Extract<MemoryReceipt, { kind: "remembered" }>

function RememberedChip({ partId, receipt }: { partId: string; receipt: Remembered }) {
  const { t } = useTranslation("chat")
  const errorMessage = useApiErrorMessage()
  const [state, setState] = useState<"kept" | "undoing" | "undone">("kept")
  const undo = async () => {
    if (state !== "kept") return
    setState("undoing")
    try {
      // One request id per receipt: a retry after a lost response is a no-op.
      await memoryApi.forget({ id: receipt.memoryId, revision: receipt.revision }, `assistant-undo:${partId}`)
      setState("undone")
    } catch (error) {
      setState("kept")
      toast("error", error instanceof ApiError && error.code === "MEMORY_REVISION_CONFLICT"
        ? t("assistant.memory.undoChanged") : errorMessage(error))
    }
  }
  return (
    <span className={CHIP} title={receipt.summary}>
      {state === "undone" ? <BookmarkX className={ICON} strokeWidth={1.6} aria-hidden />
        : <BookmarkCheck className={ICON} strokeWidth={1.6} aria-hidden />}
      <span className={cn("min-w-0 truncate", state === "undone" && "line-through")}>
        {t("assistant.memory.remembered", { summary: receipt.summary })}
      </span>
      <span aria-hidden>·</span>
      <button type="button" disabled={state !== "kept"} onClick={() => void undo()}
        className="text-ink flex-none underline-offset-2 hover:underline disabled:text-n600 disabled:no-underline">
        {t(state === "undone" ? "assistant.memory.undone" : state === "undoing" ? "assistant.memory.undoing" : "assistant.memory.undo")}
      </button>
    </span>
  )
}

function Chip({ receipt }: { receipt: Exclude<MemoryReceipt, Remembered> }) {
  const { t } = useTranslation("chat")
  const kept = receipt.kind === "already_remembered" || receipt.kind === "updated"
  const label = receipt.kind === "updated" ? t("assistant.memory.updated", { summary: receipt.summary })
    : t(`assistant.memory.${receipt.kind === "already_remembered" ? "alreadyRemembered" : receipt.kind}`)
  return (
    <span className={CHIP} title={receipt.kind === "updated" ? receipt.summary : undefined}>
      {kept ? <BookmarkCheck className={ICON} strokeWidth={1.6} aria-hidden />
        : <BookmarkX className={ICON} strokeWidth={1.6} aria-hidden />}
      <span className="min-w-0 truncate">{label}</span>
    </span>
  )
}

export function AssistantMemoryReceipts({ parts }: { parts: MessagePart[] }) {
  const { t } = useTranslation("chat")
  const receipts = parts.filter((part): part is ToolPart => part.type === "tool").flatMap((part) => {
    const receipt = memoryReceipt(part)
    return receipt ? [{ partId: part.id, receipt }] : []
  })
  if (receipts.length === 0) return null
  return (
    <div role="group" aria-label={t("assistant.memory.label")} className="mt-2 flex flex-wrap gap-1.5">
      {receipts.map(({ partId, receipt }) => receipt.kind === "remembered"
        ? <RememberedChip key={partId} partId={partId} receipt={receipt} />
        : <Chip key={partId} receipt={receipt} />)}
    </div>
  )
}
