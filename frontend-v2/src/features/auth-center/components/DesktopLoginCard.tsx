// 云电脑登录态 — sites the person logged into on their cloud desktop.
//
// Rows come from two sources: the site catalogue (every site is listed even
// when nobody logged in yet) and the desktop_cookie accounts the backend
// registered by probing. "去登录" pushes the login page to the desktop, opens
// the desktop panel, and polls until the probe says bound.
import { useEffect, useMemo, useRef, useState } from "react"
import { useTranslation } from "react-i18next"
import { LogOut, Monitor, RefreshCw } from "lucide-react"
import { cn } from "@/shared/lib/cn"
import { requestDesktopPanel } from "@/shared/events/desktop"
import type { Platform, PlatformAccount } from "../types"

interface Props {
  sites: Platform[]
  accounts: PlatformAccount[]
  canManage: boolean
  busy: boolean
  /** Which site is being polled after 去登录, if any. */
  awaitingSite: string | null
  onOpenLogin: (site: string) => void
  onProbe: (id: string) => void
  onProbeAll: () => void
  onLogout: (account: PlatformAccount) => void
}

type RowStatus = "bound" | "expired" | "unknown" | "desktop_offline" | "revoked" | "none"

const STATUS_CLASS: Record<RowStatus, string> = {
  bound: "bg-a200 text-a700",
  expired: "bg-dangersoft text-dangerink",
  unknown: "bg-n200 text-n700",
  desktop_offline: "bg-n200 text-n700",
  revoked: "bg-n200 text-n700",
  none: "bg-n200 text-n700",
}

function fmt(iso: string | null | undefined): string {
  if (!iso) return "—"
  return new Date(iso).toLocaleString()
}

function fmtDay(iso: string | null | undefined): string {
  if (!iso) return "—"
  return new Date(iso).toLocaleDateString()
}

function daysUntil(iso: string | null | undefined): number | null {
  if (!iso) return null
  return Math.floor((new Date(iso).getTime() - Date.now()) / 86_400_000)
}

export function DesktopLoginCard({
  sites,
  accounts,
  canManage,
  busy,
  awaitingSite,
  onOpenLogin,
  onProbe,
  onProbeAll,
  onLogout,
}: Props) {
  const { t } = useTranslation("auth-center")
  const bySite = useMemo(() => {
    const map = new Map<string, PlatformAccount>()
    for (const a of accounts) if (a.authKind === "desktop_cookie") map.set(a.platform, a)
    return map
  }, [accounts])
  const desktopId = accounts.find((a) => a.authKind === "desktop_cookie")?.desktopId ?? null

  return (
    <section className="border-hair bg-card flex flex-col gap-3 rounded-2xl border p-4.5">
      <header className="flex flex-wrap items-center gap-2">
        <Monitor size={18} className="text-n700" />
        <span className="text-ink text-lg font-medium">{t("desktop.title")}</span>
        {desktopId ? (
          <span className="text-n600 truncate text-xs" title={desktopId}>
            {t("desktop.desktopId", { id: desktopId.slice(-8) })}
          </span>
        ) : null}
        <span className="flex-1" />
        <button
          type="button"
          disabled={busy}
          onClick={onProbeAll}
          className="text-n700 hover:bg-hairsoft flex items-center gap-1.5 rounded-full px-3 py-1.5 text-sm disabled:opacity-50"
        >
          <RefreshCw size={14} className={cn(busy && "animate-spin")} />
          {t("desktop.probeAll")}
        </button>
      </header>
      <p className="text-n600 text-xs">{t("desktop.subtitle")}</p>

      <div className="flex flex-col gap-2">
        {sites.map((site) => (
          <DesktopSiteRow
            key={site.key}
            site={site}
            row={bySite.get(site.key) ?? null}
            awaiting={awaitingSite === site.key}
            busy={busy}
            canManage={canManage}
            onOpenLogin={onOpenLogin}
            onProbe={onProbe}
            onLogout={onLogout}
          />
        ))}
      </div>
    </section>
  )
}

const NONE: RowStatus = "none"

interface RowProps {
  site: Platform
  row: PlatformAccount | null
  awaiting: boolean
  busy: boolean
  canManage: boolean
  onOpenLogin: (site: string) => void
  onProbe: (id: string) => void
  onLogout: (account: PlatformAccount) => void
}

function RowMeta({ site, row, status, awaiting }: { site: Platform; row: PlatformAccount | null; status: RowStatus; awaiting: boolean }) {
  const { t } = useTranslation("auth-center")
  const predicted = row?.predictedExpiresAt ?? null
  const display = row?.probeDetail?.display ?? null
  return (
    <div className="flex min-w-0 flex-1 flex-col gap-1">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-ink text-base font-medium">{site.display}</span>
        <span className={cn("rounded-full px-2 py-0.5 text-2xs font-medium", STATUS_CLASS[status])}>
          {awaiting ? t("desktop.status.awaiting") : t(`desktop.status.${status}`)}
        </span>
        {row?.nickname ? <span className="text-n700 truncate text-xs">{row.nickname}</span> : null}
        {site.reconPending ? <span className="text-n600 text-2xs">{t("desktop.reconPending")}</span> : null}
      </div>
      {status === "bound" && predicted ? (
        <span className="text-ink text-sm">
          {t("desktop.predicted", { date: fmtDay(predicted), days: Math.max(0, daysUntil(predicted) ?? 0) })}
        </span>
      ) : null}
      <div className="text-n600 flex flex-wrap gap-x-4 gap-y-0.5 text-xs">
        {display?.account_name ? <span>{t("desktop.display.account", { name: String(display.account_name) })}</span> : null}
        {display?.role ? <span>{String(display.role)}</span> : null}
        {row ? <span>{t("account.lastProbe", { date: fmt(row.lastProbeAt) })}</span> : null}
        {awaiting ? <span className="text-ink">{t("desktop.awaitingHint")}</span> : null}
      </div>
      {row?.lastError && status !== "bound" ? (
        <span className="text-danger truncate text-xs" title={row.lastError}>
          {row.lastError}
        </span>
      ) : null}
    </div>
  )
}

function RowActions({ site, row, status, awaiting, busy, canManage, onOpenLogin, onProbe, onLogout }: RowProps & { status: RowStatus }) {
  const { t } = useTranslation("auth-center")
  const bound = status === "bound" && row !== null
  return (
    <div className="flex flex-none items-center gap-1">
      {bound ? (
        <button
          type="button"
          disabled={busy}
          onClick={() => onProbe(row.id)}
          className="text-n700 hover:bg-hairsoft flex items-center gap-1.5 rounded-full px-3 py-1.5 text-sm disabled:opacity-50"
        >
          <RefreshCw size={14} />
          {t("actions.probe")}
        </button>
      ) : (
        <button
          type="button"
          disabled={busy || awaiting || Boolean(site.reconPending)}
          onClick={() => onOpenLogin(site.key)}
          className="bg-ink text-bg rounded-full px-3 py-1.5 text-sm hover:opacity-90 disabled:opacity-50"
        >
          {status === "expired" ? t("desktop.actions.relogin") : t("desktop.actions.login")}
        </button>
      )}
      {awaiting ? (
        <button type="button" onClick={requestDesktopPanel} className="text-n700 hover:bg-hairsoft rounded-full px-3 py-1.5 text-sm">
          {t("desktop.actions.viewDesktop")}
        </button>
      ) : null}
      {canManage && bound ? (
        <button
          type="button"
          disabled={busy}
          onClick={() => onLogout(row)}
          title={t("desktop.actions.logout")}
          aria-label={t("desktop.actions.logout")}
          className="text-n600 hover:bg-hairsoft hover:text-danger flex size-8 items-center justify-center rounded-full disabled:opacity-50"
        >
          <LogOut size={14} />
        </button>
      ) : null}
    </div>
  )
}

function DesktopSiteRow(props: RowProps) {
  const status: RowStatus = (props.row?.status as RowStatus | undefined) ?? NONE
  return (
    <div className="border-hair flex items-start gap-3 rounded-xl border p-3">
      <RowMeta site={props.site} row={props.row} status={status} awaiting={props.awaiting} />
      <RowActions {...props} status={status} />
    </div>
  )
}

/** Poll a row every 5s for up to 3 minutes after 去登录; resolves when bound. */
export function useLoginPolling(
  awaiting: { site: string; accountId: string } | null,
  probe: (id: string) => Promise<PlatformAccount>,
  onDone: (result: "bound" | "timeout") => void,
) {
  const timer = useRef<number | null>(null)
  const [tick, setTick] = useState(0)

  useEffect(() => {
    if (!awaiting) return
    const started = Date.now()
    timer.current = window.setInterval(() => {
      setTick((n) => n + 1)
      if (Date.now() - started > 180_000) {
        if (timer.current) window.clearInterval(timer.current)
        onDone("timeout")
        return
      }
      void probe(awaiting.accountId)
        .then((row) => {
          if (row.status === "bound") {
            if (timer.current) window.clearInterval(timer.current)
            onDone("bound")
          }
        })
        .catch(() => undefined)
    }, 5_000)
    return () => {
      if (timer.current) window.clearInterval(timer.current)
    }
  }, [awaiting, probe, onDone])

  return tick
}
