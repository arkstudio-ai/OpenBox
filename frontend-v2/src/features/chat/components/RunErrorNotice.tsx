// Why the last run produced nothing, kept above the composer.
//
// The toast is easy to miss and easy to dismiss, and once it is gone a failed
// turn looks exactly like a successful one that happened to be quiet — which
// reads as the app being broken rather than the request having failed. This
// line stays until the next message is sent, so there is always something to
// find. It lives outside the scroll area for the same reason the question dock
// does: it must not be scrollable away.
import { useTranslation } from "react-i18next"
import { AlertCircle, X } from "lucide-react"

export function RunErrorNotice({
  message,
  onDismiss,
}: {
  message: string
  onDismiss: () => void
}) {
  const { t } = useTranslation("common")

  return (
    <div
      role="status"
      className="mx-auto flex w-full max-w-170 items-start gap-2 px-1 pb-1.5"
    >
      <AlertCircle size={13} className="mt-0.5 flex-none text-danger" aria-hidden />
      <p className="min-w-0 flex-1 break-words text-xs leading-5 text-danger">{message}</p>
      <button
        type="button"
        onClick={onDismiss}
        aria-label={t("close")}
        className="-me-0.5 flex size-5 flex-none items-center justify-center rounded text-danger/60 transition-colors hover:bg-dangersoft hover:text-danger"
      >
        <X size={12} />
      </button>
    </div>
  )
}
