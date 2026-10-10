import { useState } from "react"
import { useTranslation } from "react-i18next"
import type { MemoryRecord } from "@/shared/api/memory"
import { Dialog, DialogActions, DialogTitle } from "@/shared/ui/Dialog"
import { useMemorySources } from "../api"
import { button, dangerButton } from "./ui"

/** Confirm forgetting one memory. By default only the memory itself goes;
 *  ticking the box also clears the original wording it was learned from —
 *  never the chat, the session or the project. */
export function ForgetDialog({
  memory,
  pending,
  error,
  onConfirm,
  onClose,
}: {
  memory: MemoryRecord
  pending: boolean
  error: string | null
  /** Source ids to clear as well, or undefined to forget the memory alone. */
  onConfirm: (sourceIds?: string[]) => void
  onClose: () => void
}) {
  const { t } = useTranslation("knowledge")
  const sources = useMemorySources(memory.id)
  const [clearSources, setClearSources] = useState(false)
  const sourceIds = (sources.data?.sources ?? []).map((source) => source.id)
  return (
    <Dialog open onClose={() => !pending && onClose()} label={t("forget.title")}>
      <DialogTitle>{t("forget.title")}</DialogTitle>
      <blockquote className="bg-hairsoft text-ink max-h-40 overflow-y-auto rounded-xl px-3.5 py-2.5 text-sm leading-relaxed break-words whitespace-pre-wrap">
        {memory.summary}
      </blockquote>
      <p className="text-n700 text-sm leading-relaxed">{t("forget.body")}</p>
      {sourceIds.length > 0 && (
        <label className="text-n800 flex items-start gap-2.5 text-sm leading-relaxed">
          <input
            type="checkbox"
            className="accent-accent mt-1"
            checked={clearSources}
            disabled={pending}
            onChange={(event) => setClearSources(event.target.checked)}
          />
          <span>
            {t("forget.clearSources", { count: sourceIds.length })}
            {clearSources && <span className="text-n600 mt-0.5 block text-xs">{t("forget.clearSourcesHint")}</span>}
          </span>
        </label>
      )}
      {error && (
        <p className="bg-dangersoft text-dangerink rounded-xl px-3 py-2 text-sm" role="alert">
          {error}
        </p>
      )}
      <DialogActions>
        <button type="button" className={button} disabled={pending} onClick={onClose}>
          {t("forget.cancel")}
        </button>
        <button
          type="button"
          className={dangerButton}
          disabled={pending || (clearSources && !sourceIds.length)}
          onClick={() => onConfirm(clearSources ? sourceIds : undefined)}
        >
          {t(pending ? "forget.forgetting" : "forget.confirm")}
        </button>
      </DialogActions>
    </Dialog>
  )
}
