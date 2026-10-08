// The project brief: standing context the user and their personal assistant
// keep for one project, read by every one of the user's sessions in it.
import { useState } from "react"
import { useTranslation } from "react-i18next"
import { useAssistantNames } from "@/shared/appearance/useAssistantNames"
import { ApiError } from "@/shared/api/http"
import { useApiErrorMessage } from "@/shared/hooks/useApiErrorMessage"
import { cn } from "@/shared/lib/cn"
import { formatDateTime, formatSince } from "@/shared/lib/format"
import type { Project } from "@/shared/types/api"
import { Dialog, DialogActions, DialogTitle } from "@/shared/ui/Dialog"
import { MAX_BRIEF_CHARS, useProjectBrief, useSaveProjectBrief, type ProjectBrief } from "../api/brief"

export function ProjectBriefDialog({ project, onClose }: { project: Project | null; onClose: () => void }) {
  const { t } = useTranslation("workspace")
  return (
    <Dialog open={project !== null} onClose={onClose} wide label={t("brief.title")}>
      {project && <BriefEditor key={project.id} project={project} onClose={onClose} />}
    </Dialog>
  )
}

/** Code points, as the server counts them: an emoji is one character. */
const characters = (text: string) => [...text].length

type Notice = { kind: "saved" } | { kind: "conflict" } | { kind: "error"; text: string }

function Updated({ brief }: { brief: ProjectBrief | null | undefined }) {
  const { t } = useTranslation("workspace")
  const name = useAssistantNames().mention
  if (!brief || brief.revision === 0 || !brief.updated_at) return null
  return (
    <span title={formatDateTime(brief.updated_at)}>
      {t(brief.updated_by === "assistant" ? "brief.byAssistant" : "brief.byUser", { name })} · {formatSince(brief.updated_at)}
    </span>
  )
}

function BriefEditor({ project, onClose }: { project: Project; onClose: () => void }) {
  const { t } = useTranslation("workspace")
  const assistantName = useAssistantNames().mention
  const errorMessage = useApiErrorMessage()
  const brief = useProjectBrief(project.id)
  const save = useSaveProjectBrief(project.id)
  // The draft starts from a fresh read, never a cached copy. From then on it
  // is the person's: a later read changes only the version it is based on.
  const [draft, setDraft] = useState<string | null>(null)
  const [base, setBase] = useState<ProjectBrief | null>(null)
  const [notice, setNotice] = useState<Notice | null>(null)
  if (draft === null && brief.isFetchedAfterMount && brief.data !== undefined) {
    setDraft(brief.data?.content ?? "")
    setBase(brief.data)
  }
  const latest = brief.data
  const count = characters(draft ?? "")
  const over = count > MAX_BRIEF_CHARS
  const dirty = draft !== null && draft !== (base?.content ?? "")

  const refused = (error: Error) => {
    if (error instanceof ApiError && error.code === "PROJECT_BRIEF_TOO_LONG") return t("brief.tooLong", { max: MAX_BRIEF_CHARS })
    if (error instanceof ApiError && error.code === "PROJECT_BRIEF_SENSITIVE_CONTENT") return t("brief.sensitive")
    if (error instanceof ApiError && error.status === 404) return t("brief.notFound")
    return errorMessage(error)
  }
  const submit = () => {
    if (draft === null || !dirty || over || save.isPending) return
    setNotice(null)
    save.mutate({ content: draft, revision: base?.revision ?? 0 }, {
      onSuccess: (saved) => { setBase(saved); setNotice({ kind: "saved" }) },
      onError: async (error) => {
        if (!(error instanceof ApiError && error.status === 409)) { setNotice({ kind: "error", text: refused(error) }); return }
        // Someone (usually the assistant) saved first. Load that version and
        // keep the person's text: saving again replaces it on purpose.
        const reread = await brief.refetch()
        if (reread.data !== undefined && !reread.error) {
          setBase(reread.data)
          setNotice({ kind: "conflict" })
        } else setNotice({ kind: "error", text: errorMessage(reread.error) })
      },
    })
  }

  return (
    <>
      <DialogTitle>{t("brief.title")}</DialogTitle>
      <p className="text-n600 flex flex-wrap gap-x-2 text-sm">
        <span className="text-ink font-medium">{project.name}</span>
        <Updated brief={latest} />
      </p>
      {draft === null ? (
        brief.error ? (
          <div role="alert" className="text-sm">
            <p>{refused(brief.error)}</p>
            <button type="button" className="mt-2 underline" onClick={() => void brief.refetch()}>
              {t("common:action.retry", { ns: "common" })}
            </button>
          </div>
        ) : (
          <p role="status" className="text-n600 text-sm">{t("brief.loading")}</p>
        )
      ) : (
        <>
          {(!base || base.revision === 0) && <p className="text-n700 text-sm">{t("brief.empty", { name: assistantName })}</p>}
          <textarea
            value={draft}
            onChange={(event) => { setDraft(event.target.value); if (notice?.kind === "saved") setNotice(null) }}
            aria-label={t("brief.label")}
            placeholder={t("brief.placeholder")}
            rows={14}
            className="border-hair bg-bg text-ink placeholder:text-n500 focus:border-accent min-h-60 w-full resize-y rounded-lg border px-3 py-2 text-sm leading-6 outline-none"
          />
          <div className="flex items-center justify-between gap-3 text-xs">
            <span className="text-n600">{t("brief.hint")}</span>
            <span className={cn("tabular-nums", over ? "text-dangerink font-medium" : "text-n600")} aria-live="polite">
              {t("brief.count", { count, max: MAX_BRIEF_CHARS })}
            </span>
          </div>
          {notice?.kind === "saved" && <p role="status" className="text-n600 text-sm">{t("brief.saved")}</p>}
          {notice?.kind === "error" && <p role="alert" className="text-dangerink text-sm">{notice.text}</p>}
          {notice?.kind === "conflict" && (
            <div role="alert" className="border-hair bg-n100/60 rounded-lg border px-3 py-2 text-sm">
              <p>{t("brief.conflict", { name: assistantName })}</p>
              <button type="button" className="mt-1 underline" onClick={() => { setDraft(base?.content ?? ""); setNotice(null) }}>
                {t("brief.showLatest")}
              </button>
            </div>
          )}
        </>
      )}
      <DialogActions>
        <button type="button" className="text-n700 text-base" onClick={onClose}>
          {t("common:action.close", { ns: "common" })}
        </button>
        <button
          type="button"
          className="bg-ink text-bg rounded-full px-4.5 py-2 text-base disabled:opacity-40"
          disabled={!dirty || over || save.isPending}
          onClick={submit}
        >
          {save.isPending ? t("brief.saving") : t("common:action.save", { ns: "common" })}
        </button>
      </DialogActions>
    </>
  )
}
