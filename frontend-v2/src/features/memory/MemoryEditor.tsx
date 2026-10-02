import { useState } from "react"
import { useTranslation } from "react-i18next"
import { Dialog, DialogActions, DialogTitle } from "@/shared/ui/Dialog"
import type { MemoryRecord } from "@/shared/api/memory"
import { memoryButton, memoryInput, memoryPrimary } from "@/shared/ui/MemoryDiagnostics"
import { useMemorySources } from "./api"

export type MemoryAction = "create" | "correct" | "forget"
export interface MemoryEdit {
  action: MemoryAction
  memory?: MemoryRecord
}
export interface MemoryEditInput {
  summary: string
  sourceIds?: string[]
}

export function MemoryEditor({
  edit,
  pending,
  error,
  onSubmit,
  onClose,
}: {
  edit: MemoryEdit
  pending: boolean
  error: string | null
  onSubmit: (input: MemoryEditInput) => void
  onClose: () => void
}) {
  const { t } = useTranslation("memory")
  const [summary, setSummary] = useState(edit.memory?.summary ?? "")
  const [mode, setMode] = useState<"memory" | "sources">("memory")
  const [selectedSources, setSelectedSources] = useState<string[]>([])
  const sources = useMemorySources(edit.action === "forget" ? (edit.memory?.id ?? "") : "")
  const forget = edit.action === "forget"
  const valid = forget ? mode === "memory" || selectedSources.length > 0 : summary.trim().length > 0
  return (
    <Dialog open onClose={() => !pending && onClose()} label={t(`${edit.action}Title`)}>
      <form
        onSubmit={(event) => {
          event.preventDefault()
          if (!valid || pending) return
          onSubmit({
            summary: summary.trim(),
            sourceIds: forget && mode === "sources" ? selectedSources : undefined,
          })
        }}
      >
        <DialogTitle>{t(`${edit.action}Title`)}</DialogTitle>
        <p className="text-n600 mt-2 mb-4 text-sm">{t(`${edit.action}Hint`)}</p>
        {edit.memory && (
          <p className="text-n500 mb-3 text-xs break-all">
            {edit.memory.id} · {t("revision", { revision: edit.memory.revision })}
          </p>
        )}
        {!forget && (
          <>
            <label htmlFor="memory-summary" className="mb-2 block text-sm">
              {t("content")}
            </label>
            <textarea
              id="memory-summary"
              value={summary}
              disabled={pending}
              onChange={(event) => setSummary(event.target.value)}
              maxLength={2000}
              rows={6}
              className={`${memoryInput} w-full resize-y`}
            />
            <p className="text-n500 mt-1 text-right text-xs">
              {t("characters", { count: summary.length, limit: 2000 })}
            </p>
          </>
        )}
        {forget && (
          <fieldset className="space-y-3 text-sm" disabled={pending}>
            <legend className="mb-2 font-medium">{t("forgetScope")}</legend>
            <label className="flex items-start gap-2">
              <input
                type="radio"
                name="forget-mode"
                checked={mode === "memory"}
                onChange={() => setMode("memory")}
                className="mt-1"
              />
              {t("forgetMemoryOnly")}
            </label>
            <label className="flex items-start gap-2">
              <input
                type="radio"
                name="forget-mode"
                checked={mode === "sources"}
                onChange={() => setMode("sources")}
                disabled={!sources.data?.sources.length}
                className="mt-1"
              />
              {t("forgetSelectedSources")}
            </label>
            {mode === "sources" && (
              <div className="border-hair space-y-2 rounded-lg border p-3">
                {sources.data?.sources.map((source) => (
                  <label key={source.id} className="flex items-start gap-2 text-xs break-all">
                    <input
                      type="checkbox"
                      checked={selectedSources.includes(source.id)}
                      onChange={(event) =>
                        setSelectedSources((current) =>
                          event.target.checked
                            ? [...current, source.id]
                            : current.filter((id) => id !== source.id),
                        )
                      }
                    />
                    {source.id}
                  </label>
                ))}
              </div>
            )}
            <p className="text-n600">{t("forgetBoundedHint")}</p>
          </fieldset>
        )}
        {error && (
          <p className="text-danger mt-3 text-sm" role="alert">
            {error}
          </p>
        )}
        <DialogActions>
          <button type="button" className={memoryButton} disabled={pending} onClick={onClose}>
            {t("cancel")}
          </button>
          <button type="submit" className={memoryPrimary} disabled={!valid || pending}>
            {t(pending ? "saving" : forget ? "forgetConfirm" : "save")}
          </button>
        </DialogActions>
      </form>
    </Dialog>
  )
}
