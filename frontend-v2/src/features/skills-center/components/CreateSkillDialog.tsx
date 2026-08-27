import { useState } from "react"
import { useTranslation } from "react-i18next"
import type { Project } from "@/shared/types/api"

interface Props {
  projects: Project[]
  loading: boolean
  busy: boolean
  error?: string | null
  onCancel: () => void
  onConfirm: (projectId: string, brief: string) => void
}

/** Starts a normal conversation; the agent, not this form, designs the skill. */
export function CreateSkillDialog({ projects, loading, busy, error, onCancel, onConfirm }: Props) {
  const { t } = useTranslation("skills")
  const [projectId, setProjectId] = useState("")
  const [brief, setBrief] = useState("")
  const selectedProject = projectId || projects[0]?.id || ""
  const canSubmit = Boolean(selectedProject && brief.trim()) && !loading

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/35 p-4"
      role="dialog"
      aria-modal="true"
      aria-label={t("create.title")}
    >
      <form
        className="border-hair bg-card w-full max-w-[500px] rounded-2xl border p-5 shadow-xl"
        onSubmit={(event) => {
          event.preventDefault()
          if (canSubmit) onConfirm(selectedProject, brief.trim())
        }}
      >
        <h2 className="text-ink text-base font-medium">{t("create.title")}</h2>
        <p className="text-n600 mt-1 text-xs leading-5">{t("create.subtitle")}</p>

        <label className="mt-4 block">
          <span className="text-n600 text-xs">{t("create.projectLabel")}</span>
          <select
            value={selectedProject}
            onChange={(event) => setProjectId(event.target.value)}
            disabled={loading || busy || projects.length === 0}
            className="border-hair bg-canvas text-ink focus:border-accent mt-1 w-full rounded-lg border px-2.5 py-2 text-sm outline-none disabled:opacity-50"
          >
            {projects.length === 0 ? <option value="">{t("create.noProjects")}</option> : null}
            {projects.map((project) => (
              <option key={project.id} value={project.id}>
                {project.name}
              </option>
            ))}
          </select>
        </label>

        <label className="mt-3 block">
          <span className="text-n600 text-xs">{t("create.briefLabel")}</span>
          <textarea
            value={brief}
            onChange={(event) => setBrief(event.target.value)}
            rows={5}
            autoFocus
            disabled={busy}
            placeholder={t("create.briefPlaceholder")}
            className="border-hair bg-canvas text-ink placeholder:text-n600 focus:border-accent mt-1 w-full resize-none rounded-lg border px-2.5 py-2 text-sm leading-6 outline-none disabled:opacity-50"
          />
        </label>
        <p className="text-n600 mt-1.5 text-xs leading-5">{t("create.chatHint")}</p>

        {error ? (
          <p className="bg-dangersoft text-danger mt-3 rounded-lg px-3 py-2 text-xs leading-5">{error}</p>
        ) : null}

        <div className="mt-5 flex justify-end gap-2">
          <button
            type="button"
            onClick={onCancel}
            disabled={busy}
            className="text-n700 hover:bg-hairsoft rounded-full px-3.5 py-1.5 text-sm disabled:opacity-50"
          >
            {t("common.cancel")}
          </button>
          <button
            type="submit"
            disabled={busy || !canSubmit}
            className="bg-ink text-bg rounded-full px-3.5 py-1.5 text-sm hover:opacity-90 disabled:opacity-50"
          >
            {busy ? t("create.starting") : t("create.confirm")}
          </button>
        </div>
      </form>
    </div>
  )
}
