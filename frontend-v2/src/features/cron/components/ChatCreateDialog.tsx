import { useState } from "react"
import { useTranslation } from "react-i18next"
import { useNavigate } from "react-router"
import { useMutation } from "@tanstack/react-query"
import { http } from "@/shared/api/http"
import { paths } from "@/shared/router/paths"
import { cn } from "@/shared/lib/cn"
import { Dialog, DialogActions, DialogBody, DialogTitle } from "@/shared/ui/Dialog"
import { useProjectOptions } from "@/features/cron/api/projects"

const inputCls =
  "min-h-9 rounded-lg border border-hair bg-card px-3 text-sm text-ink outline-none focus:border-ink"

interface CreatedSession {
  id: string
}

/** "Create via chat": pick a project, then open a fresh conversation in it
 *  seeded with the guided-setup prompt (the scheduled-tasks skill takes over
 *  on the agent side). */
export function ChatCreateDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { t } = useTranslation("cron")
  const navigate = useNavigate()
  const projects = useProjectOptions()
  const [projectId, setProjectId] = useState("")
  const [failed, setFailed] = useState(false)

  const start = useMutation({
    mutationFn: async (targetProject: string) => {
      const session = await http.post<CreatedSession>("/api/agent/session", {
        project_id: targetProject,
        title: t("chatCreate.sessionTitle"),
      })
      await http.post(`/api/agent/session/${session.id}/prompt_async`, {
        text: t("chatCreate.prompt"),
      })
      return session
    },
    onSuccess: (session) => {
      onClose()
      navigate(paths.chat(session.id))
    },
    onError: () => setFailed(true),
  })

  const targetProject = projectId || projects.data?.[0]?.id || ""

  if (!open) return null
  return (
    <Dialog open onClose={onClose}>
      <DialogTitle>{t("chatCreate.title")}</DialogTitle>
      <DialogBody>{t("chatCreate.body")}</DialogBody>

      <label className="mt-1 flex flex-col gap-1.5">
        <span className="text-xs text-n600">{t("form.project")}</span>
        <select
          className={inputCls}
          value={targetProject}
          onChange={(e) => setProjectId(e.target.value)}
        >
          {(projects.data ?? []).map((p) => (
            <option key={p.id} value={p.id}>
              {p.name?.trim() || p.id}
            </option>
          ))}
        </select>
      </label>

      {failed && <span className="text-pretty text-xs text-danger">{t("chatCreate.failed")}</span>}

      <DialogActions>
        <button type="button" onClick={onClose} className="min-h-9 rounded-full px-4 text-sm text-n700">
          {t("form.cancel")}
        </button>
        <button
          type="button"
          disabled={!targetProject || start.isPending}
          onClick={() => start.mutate(targetProject)}
          className={cn(
            "min-h-9 rounded-full bg-ink px-5 text-sm text-bg",
            (!targetProject || start.isPending) && "opacity-50",
          )}
        >
          {start.isPending ? t("chatCreate.starting") : t("chatCreate.start")}
        </button>
      </DialogActions>
    </Dialog>
  )
}
