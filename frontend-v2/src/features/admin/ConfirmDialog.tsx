import { useTranslation } from "react-i18next"
import { Dialog, DialogActions, DialogBody, DialogTitle } from "@/shared/ui/Dialog"
import { cn } from "@/shared/lib/cn"

export interface FleetConfirm {
  /** The prompt sentence — the very same locale string the old confirm showed. */
  body: string
  /** Runs once the operator confirms; the dialog closes either way. */
  run: () => void
  /** Destructive actions get the danger button; replenishment does not. */
  danger?: boolean
}

/**
 * The fleet page used `window.confirm`, which the console spec bans (§4.6.2)
 * because a native dialog ignores the design language and can be suppressed by
 * the browser. One holder for every fleet prompt: the call sites only hand over
 * a sentence and a callback, so no wording or behaviour changed with it.
 */
export function ConfirmDialog({ pending, onClose }: { pending: FleetConfirm | null; onClose: () => void }) {
  const { t } = useTranslation("admin")
  return (
    <Dialog open={pending !== null} onClose={onClose}>
      <DialogTitle>{t("confirm.title")}</DialogTitle>
      <DialogBody>{pending?.body}</DialogBody>
      <DialogActions>
        <button type="button" className="text-n700 text-base" onClick={onClose}>
          {t("common:action.cancel", { ns: "common" })}
        </button>
        <button
          type="button"
          className={cn(
            "text-bg rounded-full px-4.5 py-2 text-base",
            pending?.danger ? "bg-danger" : "bg-ink",
          )}
          onClick={() => {
            pending?.run()
            onClose()
          }}
        >
          {t("common:action.confirm", { ns: "common" })}
        </button>
      </DialogActions>
    </Dialog>
  )
}
