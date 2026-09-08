// One bound account. Presentational; every action is a callback.
import { useTranslation } from "react-i18next"
import { RefreshCw, Trash2, UserRound } from "lucide-react"
import { cn } from "@/shared/lib/cn"
import type { PlatformAccount } from "../types"

interface Props {
  account: PlatformAccount
  canManage: boolean
  busy: boolean
  onProbe: (id: string) => void
  onReauthorize: (platform: string) => void
  onUnbind: (account: PlatformAccount) => void
}

const STATUS_CLASS: Record<PlatformAccount["status"], string> = {
  bound: "bg-a200 text-a700",
  expired: "bg-dangersoft text-dangerink",
  revoked: "bg-n200 text-n700",
  unknown: "bg-n200 text-n700",
  desktop_offline: "bg-n200 text-n700",
}

function daysUntil(iso: string | null): number | null {
  if (!iso) return null
  const ms = new Date(iso).getTime() - Date.now()
  return Math.floor(ms / 86_400_000)
}

function fmt(iso: string | null): string {
  if (!iso) return "—"
  return new Date(iso).toLocaleString()
}

function fmtDay(iso: string | null): string {
  if (!iso) return "—"
  return new Date(iso).toLocaleDateString()
}

export function AccountRow({ account, canManage, busy, onProbe, onReauthorize, onUnbind }: Props) {
  const { t } = useTranslation("auth-center")
  const refreshDays = daysUntil(account.refreshExpiresAt)
  const expiringSoon = account.status === "bound" && refreshDays !== null && refreshDays <= 7 && account.renewalsLeft === 0
  const statusKey = expiringSoon ? "expiring" : account.status

  return (
    <div className="border-hair flex items-start gap-3 rounded-xl border p-3">
      {account.avatarUrl ? (
        <img
          src={account.avatarUrl}
          alt=""
          referrerPolicy="no-referrer"
          className="bg-n200 size-10 flex-none rounded-full object-cover"
        />
      ) : (
        <span className="bg-n200 text-n600 flex size-10 flex-none items-center justify-center rounded-full">
          <UserRound size={18} strokeWidth={2} />
        </span>
      )}

      <div className="flex min-w-0 flex-1 flex-col gap-1">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-ink truncate text-base font-medium">
            {account.nickname || t("account.unnamed")}
          </span>
          <span
            className={cn(
              "rounded-full px-2 py-0.5 text-2xs font-medium",
              expiringSoon ? "bg-dangersoft text-dangerink" : STATUS_CLASS[account.status],
            )}
          >
            {t(`status.${statusKey}`)}
          </span>
          <span className="text-n600 truncate text-xs" title={account.externalId}>
            {t("account.openId", { id: account.externalId.slice(0, 8) })}
          </span>
        </div>

        {account.status === "bound" ? (
          <span className="text-ink text-sm">
            {t("account.estimatedExpiry", {
              date: fmtDay(account.estimatedExpiresAt ?? account.refreshExpiresAt),
              days: Math.max(0, daysUntil(account.estimatedExpiresAt ?? account.refreshExpiresAt) ?? 0),
            })}
          </span>
        ) : null}
        <div className="text-n600 flex flex-wrap gap-x-4 gap-y-0.5 text-xs">
          {account.status === "bound" ? (
            <>
              <span>{t("account.validUntil", { date: fmt(account.refreshExpiresAt) })}</span>
              <span>{t("account.renewalsLeft", { count: account.renewalsLeft })}</span>
            </>
          ) : (
            <span>{t("account.needsReauth")}</span>
          )}
          <span>{t("account.lastProbe", { date: fmt(account.lastProbeAt) })}</span>
        </div>
        {account.lastError && account.status !== "bound" ? (
          <span className="text-danger truncate text-xs" title={account.lastError}>
            {account.lastError}
          </span>
        ) : null}
      </div>

      <div className="flex flex-none items-center gap-1">
        {account.status === "bound" ? (
          <button
            type="button"
            disabled={busy}
            onClick={() => onProbe(account.id)}
            title={t("actions.probe")}
            className="text-n700 hover:bg-hairsoft flex items-center gap-1.5 rounded-full px-3 py-1.5 text-sm disabled:opacity-50"
          >
            <RefreshCw size={14} className={cn(busy && "animate-spin")} />
            {t("actions.probe")}
          </button>
        ) : canManage ? (
          <button
            type="button"
            disabled={busy}
            onClick={() => onReauthorize(account.platform)}
            className="bg-ink text-bg rounded-full px-3 py-1.5 text-sm hover:opacity-90 disabled:opacity-50"
          >
            {t("actions.reauthorize")}
          </button>
        ) : null}
        {canManage ? (
          <button
            type="button"
            disabled={busy}
            onClick={() => onUnbind(account)}
            title={t("actions.unbind")}
            aria-label={t("actions.unbind")}
            className="text-n600 hover:bg-hairsoft hover:text-danger flex size-8 items-center justify-center rounded-full disabled:opacity-50"
          >
            <Trash2 size={14} />
          </button>
        ) : null}
      </div>
    </div>
  )
}
