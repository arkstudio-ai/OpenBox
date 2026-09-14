import { useTranslation } from "react-i18next"
import { Dialog, DialogActions, DialogBody, DialogTitle } from "@/shared/ui/Dialog"
import { cn } from "@/shared/lib/cn"
import { button } from "../lib/styles"

export interface PendingAction {
  title: string
  body: string
  run: () => void
  danger?: boolean
}

/** One holder for publish/revoke prompts; call sites hand over copy and the action. */
export function ConfirmAction({ pending, onClose }: { pending: PendingAction | null; onClose: () => void }) {
  const { t } = useTranslation("common")
  return (
    <Dialog open={pending !== null} onClose={onClose} label={pending?.title}>
      <DialogTitle>{pending?.title}</DialogTitle>
      <DialogBody>
        <p className="text-n700 text-sm">{pending?.body}</p>
      </DialogBody>
      <DialogActions>
        <button type="button" className={button} onClick={onClose}>
          {t("action.cancel")}
        </button>
        <button
          type="button"
          className={cn(
            "text-bg min-h-9 rounded-full px-4 text-sm",
            pending?.danger ? "bg-danger" : "bg-ink",
          )}
          onClick={() => {
            pending?.run()
            onClose()
          }}
        >
          {t("action.confirm")}
        </button>
      </DialogActions>
    </Dialog>
  )
}
