import { useId } from "react"
import { useTranslation } from "react-i18next"
import { Dialog, DialogActions, DialogTitle } from "@/shared/ui/Dialog"
import { formatWhen } from "@/features/admin-billing/lib/display"
import {
  useBillingActionForm,
  type BillingFormProps,
} from "@/features/admin-billing/hooks/useBillingActionForm"

export function BillingActionDialog(props: BillingFormProps & { onClose: () => void }) {
  const { detail, kind, term, onClose } = props
  const { t } = useTranslation("admin-billing")
  const id = useId()
  const {
    credits,
    setCredits,
    plan,
    setPlan,
    cycle,
    setCycle,
    end,
    setEnd,
    reason,
    setReason,
    confirmed,
    setConfirmed,
    frozen,
    busy,
    fixed,
    valid,
    errorCode,
    error,
    submit,
  } = useBillingActionForm(props)
  const field = "w-full rounded-lg border border-hair bg-bg px-3 py-2 text-sm text-ink disabled:opacity-60"

  return (
    <Dialog
      open
      onClose={() => {
        if (!busy) onClose()
      }}
      label={t(`actions.${kind}`)}
    >
      <DialogTitle>{t(`actions.${kind}`)}</DialogTitle>
      <div className="bg-bg rounded-lg p-3 text-sm">
        <p className="font-medium">
          {detail.owner?.username} · {detail.workspace.name}
        </p>
        <p className="text-n600 mt-1 text-xs">{t("actions.currentBalance", { balance: detail.balance })}</p>
      </div>
      <p className="text-n600 text-xs leading-relaxed">{t(`actions.${kind}Hint`)}</p>
      {kind === "grant" && detail.queued.length > 0 && (
        <p className="text-n600 text-xs">
          {t("actions.queuedUntil", { time: formatWhen(detail.queued.at(-1)?.ends_at) })}
        </p>
      )}
      {kind === "credits" && (
        <label htmlFor={`${id}-credits`} className="text-n700 text-xs">
          {t("actions.creditsAmount")}
          <input
            id={`${id}-credits`}
            inputMode="decimal"
            autoComplete="off"
            disabled={fixed}
            value={credits}
            onChange={(e) => {
              setCredits(e.target.value)
              setConfirmed(false)
            }}
            className={`${field} mt-1`}
          />
          <span className="text-n500 mt-1 block">{t("actions.creditsLimit")}</span>
        </label>
      )}
      {(kind === "grant" || kind === "change") && (
        <label htmlFor={`${id}-plan`} className="text-n700 text-xs">
          {t("filters.plan")}
          <select
            id={`${id}-plan`}
            value={plan}
            disabled={fixed}
            className={`${field} mt-1`}
            onChange={(e) => {
              setPlan(e.target.value)
              setConfirmed(false)
            }}
          >
            <option value="pro">{t("plans.pro")}</option>
            <option value="max">{t("plans.max")}</option>
          </select>
        </label>
      )}
      {kind === "grant" && (
        <label htmlFor={`${id}-cycle`} className="text-n700 text-xs">
          {t("actions.duration")}
          <select
            id={`${id}-cycle`}
            value={cycle}
            disabled={fixed}
            className={`${field} mt-1`}
            onChange={(e) => {
              setCycle(e.target.value)
              setConfirmed(false)
            }}
          >
            <option value="monthly">{t("actions.oneMonth")}</option>
            <option value="yearly">{t("actions.oneYear")}</option>
          </select>
        </label>
      )}
      {kind === "change" && (
        <label htmlFor={`${id}-end`} className="text-n700 text-xs">
          {t("actions.endsAt")}
          <input
            id={`${id}-end`}
            type="datetime-local"
            value={end}
            disabled={fixed}
            className={`${field} mt-1`}
            onChange={(e) => {
              setEnd(e.target.value)
              setConfirmed(false)
            }}
          />
        </label>
      )}
      {kind === "cancel" && term && (
        <p className="text-sm">
          {t(`plans.${term.plan_id}`)} · {formatWhen(term.starts_at)} → {formatWhen(term.ends_at)}
        </p>
      )}
      <label htmlFor={`${id}-reason`} className="text-n700 text-xs">
        {t("actions.reason")}
        <textarea
          id={`${id}-reason`}
          rows={2}
          maxLength={1000}
          value={reason}
          disabled={fixed}
          className={`${field} mt-1`}
          onChange={(e) => {
            setReason(e.target.value)
            setConfirmed(false)
          }}
        />
      </label>
      <label className="text-n700 flex items-start gap-2 text-xs leading-5">
        <input
          type="checkbox"
          checked={confirmed}
          disabled={fixed || !valid}
          className="mt-1 shrink-0"
          onChange={(e) => setConfirmed(e.target.checked)}
        />
        <span>
          {t("actions.confirmTarget", { user: detail.owner?.username, workspace: detail.workspace.name })}
        </span>
      </label>
      {error && (
        <p role="alert" className="text-danger text-xs">
          {t(`actionErrors.${errorCode}`, { defaultValue: t("actionErrors.default") })}
        </p>
      )}
      <DialogActions>
        <button
          type="button"
          onClick={onClose}
          disabled={busy}
          className="text-n600 text-sm disabled:opacity-40"
        >
          {t("actions.close")}
        </button>
        <button
          type="button"
          onClick={() => void submit()}
          disabled={!valid || !confirmed || busy}
          className="bg-ink text-bg rounded-full px-4 py-2 text-sm disabled:opacity-40"
        >
          {busy ? t("actions.saving") : frozen ? t("actions.retry") : t(`actions.${kind}`)}
        </button>
      </DialogActions>
    </Dialog>
  )
}
