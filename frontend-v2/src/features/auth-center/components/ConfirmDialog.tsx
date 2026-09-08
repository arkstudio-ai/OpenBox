// One-question confirmation used for unbind / sign-out.
import { useTranslation } from "react-i18next"
import { Dialog, DialogActions, DialogBody, DialogTitle } from "@/shared/ui/Dialog"

interface Props {
  open: boolean
  title: string
  body: string
  confirmLabel: string
  onCancel: () => void
  onConfirm: () => void
}

export function ConfirmDialog({ open, title, body, confirmLabel, onCancel, onConfirm }: Props) {
  const { t } = useTranslation("common")
  return (
    <Dialog open={open} onClose={onCancel}>
      <DialogTitle>{title}</DialogTitle>
      <DialogBody>{body}</DialogBody>
      <DialogActions>
        <button type="button" className="text-md text-n700" onClick={onCancel}>
          {t("action.cancel")}
        </button>
        <button
          type="button"
          className="bg-danger text-md text-bg rounded-full px-4.5 py-2 font-medium"
          onClick={onConfirm}
        >
          {confirmLabel}
        </button>
      </DialogActions>
    </Dialog>
  )
}
