import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useTranslation } from "react-i18next"
import { Check, Settings2 } from "lucide-react"
import { memoryApi } from "@/shared/api/memory"
import { useApiErrorMessage } from "@/shared/hooks/useApiErrorMessage"
import { Dialog, DialogActions, DialogTitle } from "@/shared/ui/Dialog"
import { Menu, MenuItem } from "@/shared/ui/Menu"
import { toast } from "@/shared/ui/Toast"
import { useMemoryScope } from "../api"
import { button, dangerButton } from "./ui"

/** The person's controls over memory: whether chats are remembered at all,
 *  taking everything with them, and clearing it. */
export function KnowledgeMenu({ projectId, scopeName }: { projectId: string; scopeName: string | null }) {
  const { t, i18n } = useTranslation("knowledge")
  const errorText = useApiErrorMessage()
  const qc = useQueryClient()
  const { key } = useMemoryScope()
  const [open, setOpen] = useState(false)
  const [clearing, setClearing] = useState(false)
  const settings = useQuery({ queryKey: [...key, "memory-settings"], queryFn: () => memoryApi.settings() })
  const autoSave = settings.data?.auto_save ?? true
  const toggle = useMutation({
    mutationFn: () => memoryApi.setAutoSave(!autoSave),
    onSuccess: (next) => {
      qc.setQueryData([...key, "memory-settings"], next)
      toast.success(t(next.auto_save ? "manage.autoSaveOn" : "manage.autoSaveOff"))
    },
    onError: (error) => toast.error(errorText(error)),
  })
  const exportAll = useMutation({
    mutationFn: async () => {
      const { blob } = await memoryApi.exportAll(i18n.language)
      const url = URL.createObjectURL(blob)
      const anchor = document.createElement("a")
      anchor.href = url
      anchor.download = t("manage.exportFile")
      anchor.click()
      setTimeout(() => URL.revokeObjectURL(url), 1000)
    },
    onError: (error) => toast.error(errorText(error)),
  })
  return (
    <div className="relative">
      <button
        type="button"
        className={button}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
      >
        <Settings2 size={15} aria-hidden />
        {t("manage.title")}
      </button>
      <Menu
        open={open}
        onClose={() => setOpen(false)}
        className="start-0 top-full mt-2 w-72 max-w-[calc(100vw-2rem)]"
      >
        <MenuItem
          onClick={() => {
            setOpen(false)
            toggle.mutate()
          }}
        >
          <span className="flex items-start gap-2">
            <Check
              size={15}
              aria-hidden
              className={"mt-0.5 flex-none " + (autoSave ? "text-a700" : "invisible")}
            />
            <span>
              {t("manage.autoSave")}
              <span className="text-n600 mt-0.5 block text-xs">
                {t(autoSave ? "manage.autoSaveHint" : "manage.autoSaveOffHint")}
              </span>
            </span>
          </span>
        </MenuItem>
        <MenuItem
          onClick={() => {
            setOpen(false)
            exportAll.mutate()
          }}
        >
          <span className="ps-6">{t("manage.export")}</span>
        </MenuItem>
        <MenuItem
          danger
          onClick={() => {
            setOpen(false)
            setClearing(true)
          }}
        >
          <span className="ps-6">{t("manage.clear")}</span>
        </MenuItem>
      </Menu>
      {clearing && (
        <ClearAllDialog projectId={projectId} scopeName={scopeName} onClose={() => setClearing(false)} />
      )}
    </div>
  )
}

function ClearAllDialog({
  projectId,
  scopeName,
  onClose,
}: {
  projectId: string
  scopeName: string | null
  onClose: () => void
}) {
  const { t } = useTranslation("knowledge")
  const errorText = useApiErrorMessage()
  const qc = useQueryClient()
  const { key } = useMemoryScope()
  const [understood, setUnderstood] = useState(false)
  const clear = useMutation({
    mutationFn: () => memoryApi.forgetAll(projectId),
    onSuccess: ({ forgotten }) => {
      toast.success(t("manage.cleared", { count: forgotten }))
      void qc.invalidateQueries({ queryKey: key })
      onClose()
    },
  })
  return (
    <Dialog open onClose={() => !clear.isPending && onClose()} label={t("manage.clearTitle")}>
      <DialogTitle>{t("manage.clearTitle")}</DialogTitle>
      <p className="text-n700 text-sm leading-relaxed">
        {scopeName ? t("manage.clearScope", { scope: scopeName }) : t("manage.clearEverywhere")}
      </p>
      <label className="text-n800 flex items-start gap-2.5 text-sm leading-relaxed">
        <input
          type="checkbox"
          className="accent-accent mt-1"
          checked={understood}
          disabled={clear.isPending}
          onChange={(event) => setUnderstood(event.target.checked)}
        />
        <span>{t("manage.clearConfirm")}</span>
      </label>
      {clear.error && (
        <p role="alert" className="bg-dangersoft text-dangerink rounded-xl px-3 py-2 text-sm">
          {errorText(clear.error)}
        </p>
      )}
      <DialogActions>
        <button type="button" className={button} disabled={clear.isPending} onClick={onClose}>
          {t("forget.cancel")}
        </button>
        <button
          type="button"
          className={dangerButton}
          disabled={!understood || clear.isPending}
          onClick={() => clear.mutate()}
        >
          {t(clear.isPending ? "manage.clearing" : "manage.clearAction")}
        </button>
      </DialogActions>
    </Dialog>
  )
}
