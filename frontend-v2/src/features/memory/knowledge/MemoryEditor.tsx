import { useId, useState } from "react"
import { useTranslation } from "react-i18next"
import type { MemoryRecord } from "@/shared/api/memory"
import type { Project } from "@/shared/types/api"
import { Dialog, DialogActions, DialogTitle } from "@/shared/ui/Dialog"
import { button, field, primaryButton } from "./ui"

const LIMIT = 2000

export interface MemoryDraft {
  summary: string
  projectId: string
}

/** Add a memory, or reword one. Plain text only: no categories, scopes to
 *  configure or review steps — what is written is what the assistant keeps. */
export function MemoryEditor({
  memory,
  projects,
  projectId,
  pending,
  error,
  onSubmit,
  onClose,
}: {
  /** The memory being edited; absent when adding a new one. */
  memory?: MemoryRecord
  projects: Project[]
  projectId: string
  pending: boolean
  error: string | null
  onSubmit: (draft: MemoryDraft) => void
  onClose: () => void
}) {
  const { t } = useTranslation("knowledge")
  const ids = useId()
  const [summary, setSummary] = useState(memory?.summary ?? "")
  const [target, setTarget] = useState(projectId)
  const mode = memory ? "edit" : "create"
  const valid = summary.trim().length > 0 && summary.trim() !== memory?.summary
  return (
    <Dialog open wide onClose={() => !pending && onClose()} label={t(`editor.${mode}Title`)}>
      <form
        className="flex flex-col gap-4"
        onSubmit={(event) => {
          event.preventDefault()
          if (valid && !pending) onSubmit({ summary: summary.trim(), projectId: target })
        }}
      >
        <div>
          <DialogTitle>{t(`editor.${mode}Title`)}</DialogTitle>
          <p className="text-n600 mt-1.5 text-sm leading-relaxed">{t(`editor.${mode}Hint`)}</p>
        </div>
        <div>
          <label htmlFor={ids + "text"} className="sr-only">
            {t("editor.label")}
          </label>
          <textarea
            id={ids + "text"}
            value={summary}
            data-autofocus
            disabled={pending}
            maxLength={LIMIT}
            rows={5}
            placeholder={t("editor.placeholder")}
            onChange={(event) => setSummary(event.target.value)}
            className={field + " min-h-32 resize-y leading-relaxed"}
          />
          <div className="mt-2 flex flex-wrap items-center justify-between gap-3">
            {!memory && projects.length > 0 ? (
              <label className="text-n700 flex items-center gap-2 text-sm">
                {t("editor.saveTo")}
                <select
                  value={target}
                  disabled={pending}
                  onChange={(event) => setTarget(event.target.value)}
                  className="border-hair bg-card text-ink focus:border-accent rounded-full border px-3 py-1 text-sm outline-none"
                >
                  <option value="">{t("personal")}</option>
                  {projects.map((project) => (
                    <option key={project.id} value={project.id}>
                      {project.name}
                    </option>
                  ))}
                </select>
              </label>
            ) : (
              <span />
            )}
            <span className="text-n500 text-xs tabular-nums">
              {t("editor.characters", { count: summary.length, limit: LIMIT })}
            </span>
          </div>
        </div>
        {error && (
          <p className="bg-dangersoft text-dangerink rounded-xl px-3 py-2 text-sm" role="alert">
            {error}
          </p>
        )}
        <DialogActions>
          <button type="button" className={button} disabled={pending} onClick={onClose}>
            {t("editor.cancel")}
          </button>
          <button type="submit" className={primaryButton} disabled={!valid || pending}>
            {t(pending ? "editor.saving" : "editor.save")}
          </button>
        </DialogActions>
      </form>
    </Dialog>
  )
}
