import { useId, useState } from "react"
import { useTranslation } from "react-i18next"
import { Dialog, DialogActions, DialogBody, DialogTitle } from "@/shared/ui/Dialog"

interface Props {
  open: boolean
  /** Already interpolated with the entry title by the caller. */
  title: string
  body: string
  confirmLabel: string
  /**
   * Delist and reject notify the author with the reason, so an empty one is not
   * an option (plan §4.9); approve and relist take an optional note.
   */
  requireReason: boolean
  pending?: boolean
  failed?: boolean
  onClose: () => void
  onConfirm: (reason: string) => void
}

/** Second confirmation for every operator write, with the reason it carries. */
export function ListingDialog({
  open,
  title,
  body,
  confirmLabel,
  requireReason,
  pending,
  failed,
  onClose,
  onConfirm,
}: Props) {
  const { t } = useTranslation("admin-skills")
  const [reason, setReason] = useState("")
  const [wasOpen, setWasOpen] = useState(open)
  const [attempted, setAttempted] = useState(false)
  const fieldId = useId()

  // A reason belongs to the entry it was typed for; reopening the dialog on a
  // different row must not inherit the previous one. Adjusting during render
  // rather than in an effect keeps the empty box from flashing the old text.
  if (open !== wasOpen) {
    setWasOpen(open)
    if (open) {
      setReason("")
      setAttempted(false)
    }
  }

  const missingReason = requireReason && reason.trim() === ""
  // `failed` is the caller's mutation, which stays in its error state after it
  // settles. Only an attempt made from *this* opening may claim to have failed —
  // otherwise cancelling a failed delist and then opening a relist on another
  // row greets the operator with a red line about something they never did.
  const showFailure = failed && attempted

  return (
    <Dialog
      open={open}
      onClose={() => {
        if (!pending) onClose()
      }}
      label={title}
    >
      <DialogTitle>{title}</DialogTitle>
      <DialogBody>{body}</DialogBody>
      <label htmlFor={fieldId} className="text-n700 mt-2 text-xs">
        {requireReason ? t("dialog.reason") : t("dialog.note")}
      </label>
      <textarea
        id={fieldId}
        rows={3}
        maxLength={1000}
        disabled={pending}
        value={reason}
        onChange={(event) => setReason(event.target.value)}
        placeholder={t("dialog.reasonPlaceholder")}
        className="border-hair bg-bg text-ink placeholder:text-n600 w-full rounded-xl border px-3 py-2 text-sm outline-none"
      />
      {missingReason && <p className="text-n600 text-xs">{t("dialog.reasonRequired")}</p>}
      {showFailure && (
        <p role="alert" className="text-danger text-xs">
          {t("dialog.failed")}
        </p>
      )}
      <DialogActions>
        <button
          type="button"
          disabled={pending}
          onClick={onClose}
          className="text-n700 text-sm hover:opacity-80 disabled:opacity-40"
        >
          {t("action.cancel")}
        </button>
        <button
          type="button"
          disabled={missingReason || pending}
          onClick={() => {
            setAttempted(true)
            onConfirm(reason.trim())
          }}
          className="bg-ink text-bg rounded-full px-4 py-1.5 text-sm hover:opacity-90 disabled:opacity-40"
        >
          {confirmLabel}
        </button>
      </DialogActions>
    </Dialog>
  )
}
