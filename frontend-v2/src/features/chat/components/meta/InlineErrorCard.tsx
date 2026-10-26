// Rendered under the turn body when an assistant message carried an error.
//
// The retry button lives here rather than only in the action row below,
// because the action row is hover-revealed on desktop: a turn that produced
// nothing but this card would otherwise offer no visible way out, and the only
// recourse is retyping the prompt. A failure is exactly when an affordance
// should be obvious.
import { CircleAlert, RefreshCw, Trash2 } from "lucide-react"
import { useTranslation } from "react-i18next"
import { useApiErrorMessage } from "@/shared/hooks/useApiErrorMessage"
import { toast } from "@/shared/ui/Toast"
import { paths } from "@/shared/router/paths"
import { useDismissFailedTurn, useRegenerate, usePreserveAssistantEvidence } from "../../api/message-actions"
import { usePickedModel } from "../../stores/model-choice"

/** Both buttons read as one pair: quiet, equal weight, neither the default. */
const ACTION =
  "border-hair text-ink hover:bg-bg inline-flex items-center gap-1.5 rounded-full border bg-card px-3 py-1.5 text-xs font-medium transition-colors disabled:opacity-50"

function extractErrorMessage(error: Record<string, unknown>): string {
  const message = error.message
  if (typeof message === "string" && message.trim()) return message
  const nested = error.error
  if (typeof nested === "string" && nested.trim()) return nested
  try {
    return JSON.stringify(error)
  } catch {
    return ""
  }
}

interface Props {
  error: Record<string, unknown>
  sessionId: string
  /** The assistant message that failed — regenerating replaces it. */
  messageId: string
  /** Suppressed while the turn is live; there is nothing to retry yet. */
  streaming: boolean
}

export function InlineErrorCard({ error, sessionId, messageId, streaming }: Props) {
  const { t } = useTranslation("chat")
  const preserveEvidence = usePreserveAssistantEvidence(sessionId)
  const { mutate: regenerate, isPending } = useRegenerate(sessionId)
  const { mutate: dismiss, isPending: dismissing } = useDismissFailedTurn(sessionId)
  const errorMessage = useApiErrorMessage()
  // If the user switched the composer's picker after the failure, retry on
  // that model. Reusing the one that just failed is the case this button
  // exists for, and it is the one case where reusing it is useless.
  const picked = usePickedModel(sessionId)
  const message = extractErrorMessage(error)
  const budgetStopped = error.code === "ASSISTANT_TURN_BUDGET"
  const subscriptionBlocked = error.code === "SANDBOX_SUBSCRIPTION_REQUIRED"
  const desktopUnavailable = error.code === "SANDBOX_NOT_READY"
  const showGuidance = preserveEvidence && !budgetStopped && !subscriptionBlocked && !desktopUnavailable
  // One in flight disables both: the turn is being rewritten either way.
  const busy = isPending || dismissing

  return (
    <div className="border-hair bg-dangersoft mt-2 flex gap-3 rounded-xl border px-4 py-3">
      <CircleAlert className="text-danger mt-0.5 size-4 shrink-0" strokeWidth={1.8} />
      <div className="min-w-0 flex-1">
        <p className="text-dangerink text-md font-medium">{t(budgetStopped ? "assistant.replyIncomplete" : "meta.errorTitle")}</p>
        {showGuidance && <p className="text-n700 mt-1 text-sm">{t("assistant.continueAfterError")}</p>}
        {/* The assistant's conversations speak plainly; the raw error stays one click away. */}
        {message && (showGuidance ? (
          <details className="text-n600 mt-1 text-xs">
            <summary className="cursor-pointer">{t("meta.errorDetails")}</summary>
            <p className="mt-1 [overflow-wrap:anywhere]">{message}</p>
          </details>
        ) : <p className="text-n700 text-md mt-0.5 [overflow-wrap:anywhere]">{message}</p>)}
        {subscriptionBlocked && (
          <a href={paths.billing()} className={`${ACTION} mt-2.5`}>
            {t("workbench:activation.subscribe")}
          </a>
        )}
        {!streaming && !preserveEvidence && (
          <div className="mt-2.5 flex flex-wrap items-center gap-2">
            {error.retryable !== false && !subscriptionBlocked && <button
              type="button"
              disabled={busy}
              onClick={() =>
                regenerate(
                  { messageId, model: picked },
                  { onError: (e) => toast("error", errorMessage(e)) },
                )
              }
              className={ACTION}
            >
              <RefreshCw
                className={isPending ? "size-3.5 animate-spin" : "size-3.5"}
                strokeWidth={2}
              />
              {isPending ? t("meta.regenerating") : t("meta.regenerate")}
            </button>}
            {/* For a failure the user has already worked around — by resending
                or moving on. Left alone the card just accumulates, and the dead
                turn still rides along in every later request as context. */}
            <button
              type="button"
              disabled={busy}
              onClick={() =>
                dismiss(messageId, { onError: (e) => toast("error", errorMessage(e)) })
              }
              className={ACTION}
            >
              <Trash2 className="size-3.5" strokeWidth={2} />
              {dismissing ? t("meta.dismissing") : t("meta.dismissTurn")}
            </button>
          </div>
        )}
      </div>
    </div>
  )
}
