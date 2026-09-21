import { useState } from "react"
import { useNavigate } from "react-router"
import { useTranslation } from "react-i18next"
import type { Project } from "@/shared/types/api"
import { paths } from "@/shared/router/paths"
import { Dialog, DialogTitle, DialogActions } from "@/shared/ui/Dialog"
import { useAgentDraftGeneration, useCatalogChatCreate } from "../api/teams"
import { BUTTON, Field, INPUT, PRIMARY } from "./FormFields"

export function GenerateAgentDialog({ onClose }: { onClose: () => void }) {
  const { t } = useTranslation("teams")
  const navigate = useNavigate()
  const [prompt, setPrompt] = useState("")
  const generate = useAgentDraftGeneration()
  return (
    <Dialog open onClose={onClose} label={t("generateDraft")}>
      <DialogTitle>{t("generateDraft")}</DialogTitle>
      <p className="text-n600 text-sm">{t("generateHint")}</p>
      <Field label={t("describeAgent")} value={prompt} onChange={setPrompt} multiline maxLength={4000} />
      {generate.error && <p className="text-danger text-sm">{generate.error.message}</p>}
      <DialogActions>
        <button type="button" className={BUTTON} onClick={onClose}>
          {t("cancel")}
        </button>
        <button
          type="button"
          className={PRIMARY}
          disabled={!prompt.trim() || generate.isPending}
          onClick={() =>
            generate.mutate(
              { prompt },
              {
                onSuccess: (result) => {
                  onClose()
                  void navigate(paths.agentEditor("new"), { state: { draftSpec: result.spec } })
                },
              },
            )
          }
        >
          {t(generate.isPending ? "generating" : "generate")}
        </button>
      </DialogActions>
    </Dialog>
  )
}

export function StartChatDialog({
  templateId,
  projects,
  onClose,
}: {
  templateId: string | null
  projects: Project[]
  onClose: () => void
}) {
  const { t } = useTranslation("teams")
  const navigate = useNavigate()
  const [projectId, setProjectId] = useState("")
  const selected = projectId || projects[0]?.id || ""
  const create = useCatalogChatCreate()
  const start = () => {
    if (templateId) {
      onClose()
      void navigate(paths.newTeamChat(templateId, selected))
      return
    }
    create.mutate(
      { projectId: selected, prompt: t("chatCreatePrompt") },
      {
        onSuccess: (session) => {
          onClose()
          void navigate(paths.chat(session.id))
        },
      },
    )
  }
  return (
    <Dialog open onClose={onClose} label={t(templateId ? "runTemplate" : "chatCreate")}>
      <DialogTitle>{t(templateId ? "runTemplate" : "chatCreate")}</DialogTitle>
      <p className="text-n600 text-sm">{t(templateId ? "runTemplateHint" : "chatCreateHint")}</p>
      <label className="text-n600 mt-2 block text-xs">
        {t("project")}
        <select
          value={selected}
          onChange={(event) => setProjectId(event.target.value)}
          className={`${INPUT} mt-1.5`}
        >
          {projects.map((project) => (
            <option key={project.id} value={project.id}>
              {project.name}
            </option>
          ))}
        </select>
      </label>
      {create.error && <p className="text-danger text-sm">{create.error.message}</p>}
      <DialogActions>
        <button type="button" className={BUTTON} onClick={onClose}>
          {t("cancel")}
        </button>
        <button type="button" disabled={!selected || create.isPending} onClick={start} className={PRIMARY}>
          {t("openConversation")}
        </button>
      </DialogActions>
    </Dialog>
  )
}
