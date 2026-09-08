// One platform section: what it can do, the accounts bound to this workspace,
// and the bind button. Presentational.
import { useTranslation } from "react-i18next"
import { Plus, Send } from "lucide-react"
import { cn } from "@/shared/lib/cn"
import type { Platform, PlatformAccount } from "../types"
import { AccountRow } from "./AccountRow"

interface Props {
  platform: Platform
  accounts: PlatformAccount[]
  canManage: boolean
  busyId: string | null
  binding: boolean
  onBind: (platform: string) => void
  onProbe: (id: string) => void
  onUnbind: (account: PlatformAccount) => void
  onPublish: (platform: string) => void
}

export function PlatformCard({
  platform,
  accounts,
  canManage,
  busyId,
  binding,
  onBind,
  onProbe,
  onUnbind,
  onPublish,
}: Props) {
  const { t } = useTranslation("auth-center")
  const capabilities = platform.capabilities ?? []
  const canPublish = capabilities.includes("publish") && accounts.some((a) => a.status === "bound")

  return (
    <section className="border-hair bg-card flex flex-col gap-3 rounded-2xl border p-4.5">
      <header className="flex flex-wrap items-center gap-2">
        <span className="text-ink text-lg font-medium">{platform.display}</span>
        {capabilities.map((cap) => (
          <span key={cap} className="bg-n200 text-n700 rounded-full px-2 py-0.5 text-2xs font-medium">
            {t(`capability.${cap}`)}
          </span>
        ))}
        {!platform.configured ? (
          <span className="text-n600 text-xs">{t("platform.notConfigured")}</span>
        ) : null}
        <span className="flex-1" />
        {canPublish ? (
          <button
            type="button"
            onClick={() => onPublish(platform.key)}
            className="text-n700 hover:bg-hairsoft flex items-center gap-1.5 rounded-full px-3 py-1.5 text-sm"
          >
            <Send size={14} />
            {t("actions.publish")}
          </button>
        ) : null}
        {canManage ? (
          <button
            type="button"
            disabled={!platform.configured || binding}
            onClick={() => onBind(platform.key)}
            className={cn(
              "bg-ink text-bg flex items-center gap-1.5 rounded-full px-3 py-1.5 text-sm hover:opacity-90",
              "disabled:cursor-not-allowed disabled:opacity-50",
            )}
          >
            <Plus size={14} />
            {t("actions.bind")}
          </button>
        ) : null}
      </header>

      <p className="text-n600 text-xs">
        {platform.maxGrantDays
          ? t("platform.grantHint", { days: platform.maxGrantDays })
          : t("platform.genericHint")}
      </p>

      {accounts.length === 0 ? (
        <div className="border-hair text-n600 rounded-xl border border-dashed px-4 py-6 text-center text-sm">
          {canManage ? t("platform.emptyManager") : t("platform.emptyMember")}
        </div>
      ) : (
        <div className="flex flex-col gap-2">
          {accounts.map((account) => (
            <AccountRow
              key={account.id}
              account={account}
              canManage={canManage}
              busy={busyId === account.id}
              onProbe={onProbe}
              onReauthorize={onBind}
              onUnbind={onUnbind}
            />
          ))}
        </div>
      )}
    </section>
  )
}
