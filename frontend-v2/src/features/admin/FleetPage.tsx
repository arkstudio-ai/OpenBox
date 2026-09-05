import { useState } from "react"
import { useTranslation } from "react-i18next"
import { Spinner } from "@/shared/ui/Spinner"
import {
  useAckAlert,
  useAdoptDesktop,
  useFleetAlerts,
  useFleetDesktops,
  useFleetSnapshot,
  useMuteAlert,
  usePoolSummary,
  useRecycleDesktop,
  useReleaseDesktop,
  useRetireDesktop,
} from "./api"


const card = "rounded-xl border border-hair bg-card p-4"
const button = "rounded-full border border-hair px-3 py-1.5 text-xs text-n800 hover:bg-hairsoft disabled:opacity-50"

function date(value?: string | null) {
  return value ? new Date(value).toLocaleString() : "—"
}

export function FleetPage() {
  const { t } = useTranslation("admin")
  const pool = usePoolSummary()
  const desktops = useFleetDesktops()
  const alerts = useFleetAlerts()
  const snapshot = useFleetSnapshot()
  const ack = useAckAlert()
  const mute = useMuteAlert()
  const release = useReleaseDesktop()
  const recycle = useRecycleDesktop()
  const retire = useRetireDesktop()
  const adopt = useAdoptDesktop()
  const [adoptId, setAdoptId] = useState("")
  const [adoptState, setAdoptState] = useState<"reserve" | "prewarm">("reserve")
  const [adoptRebuild, setAdoptRebuild] = useState(false)
  const [gatewayReleaseVerified, setGatewayReleaseVerified] = useState(false)

  if ([pool, desktops, alerts, snapshot].some((query) => query.isPending)) {
    return <div className="flex justify-center py-16"><Spinner className="size-5" /></div>
  }
  if ([pool, desktops, alerts, snapshot].some((query) => query.isError)) {
    return <p className="text-sm text-danger">{t("loadFailed")}</p>
  }

  const summary = pool.data!
  return (
    <div className="flex flex-col gap-4">
      <section className={card}>
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h2 className="text-base font-medium">{t("pool.title")}</h2>
            <p className="mt-1 text-sm text-n600">
              {t("pool.watermark", {
                current: summary.states.prewarm ?? 0,
                target: summary.target_prewarm,
              })}
            </p>
          </div>
          <span className="rounded-full bg-hairsoft px-3 py-1 text-xs text-n700">
            {summary.auto_purchase ? t("pool.autoOn") : t("pool.autoOff")}
          </span>
        </div>
        <div className="mt-4 grid grid-cols-2 gap-2 text-sm sm:grid-cols-4">
          {Object.entries(summary.states).map(([state, count]) => (
            <div key={state} className="rounded-lg bg-bg px-3 py-2">
              <div className="text-n500">{state}</div>
              <div className="mt-1 text-lg font-medium">{count}</div>
            </div>
          ))}
        </div>
        <p className="mt-3 text-xs text-n500">
          {t("pool.gates", {
            price: summary.gates.max_unit_price_cny,
            tick: summary.gates.max_per_tick,
            day: summary.gates.max_per_day,
            multiple: summary.gates.min_balance_multiple,
          })}
        </p>
        <form className="mt-4 flex flex-wrap items-center gap-2" onSubmit={(event) => {
          event.preventDefault()
          const id = adoptId.trim()
          if (!id) return
          if (adoptRebuild && !window.confirm(t("pool.confirmAdoptRebuild", { id }))) return
          adopt.mutate({
            id,
            poolState: adoptState,
            rebuild: adoptRebuild,
            gatewayReleaseVerified,
          }, {
            onSuccess: () => {
              setAdoptId("")
              setAdoptRebuild(false)
              setGatewayReleaseVerified(false)
            },
          })
        }}>
          <input
            className="min-w-64 rounded-lg border border-hair bg-bg px-3 py-2 text-xs outline-none focus:border-n500"
            value={adoptId}
            onChange={(event) => setAdoptId(event.target.value)}
            placeholder={t("pool.adoptId")}
            aria-label={t("pool.adoptId")}
          />
          <select
            className="rounded-lg border border-hair bg-bg px-3 py-2 text-xs"
            value={adoptState}
            onChange={(event) => setAdoptState(event.target.value as "reserve" | "prewarm")}
          >
            <option value="reserve">{t("pool.reserveState")}</option>
            <option value="prewarm">{t("pool.prewarmState")}</option>
          </select>
          <label className="flex items-center gap-1.5 text-xs text-n600">
            <input
              type="checkbox"
              checked={adoptRebuild}
              onChange={(event) => setAdoptRebuild(event.target.checked)}
            />
            {t("pool.rebuildOnAdopt")}
          </label>
          <label className="flex items-center gap-1.5 text-xs text-n600">
            <input
              type="checkbox"
              checked={gatewayReleaseVerified}
              onChange={(event) => setGatewayReleaseVerified(event.target.checked)}
            />
            {t("pool.gatewayReleaseVerified")}
          </label>
          <button className={button} disabled={adopt.isPending || !adoptId.trim()} type="submit">
            {t("pool.adopt")}
          </button>
          {adopt.isError && <span className="text-xs text-danger">{t("pool.adoptFailed")}</span>}
        </form>
      </section>

      <section className={card}>
        <div className="flex items-baseline justify-between gap-3">
          <h2 className="text-base font-medium">{t("desktops.title")}</h2>
          <span className="text-xs text-n500">{t("desktops.count", { count: desktops.data!.total })}</span>
        </div>
        <div className="mt-3 overflow-x-auto">
          <table className="w-full min-w-[900px] text-left text-xs">
            <thead className="text-n500"><tr>
              <th className="pb-2">{t("desktops.id")}</th>
              <th className="pb-2">{t("desktops.state")}</th>
              <th className="pb-2">{t("desktops.owner")}</th>
              <th className="pb-2">{t("desktops.channel")}</th>
              <th className="pb-2">{t("desktops.billing")}</th>
              <th className="pb-2">{t("desktops.expires")}</th>
              <th className="pb-2">{t("desktops.actions")}</th>
            </tr></thead>
            <tbody>
              {desktops.data!.items.map((desktop) => (
                <tr key={desktop.id} className="border-t border-hair align-top">
                  <td className="py-2.5 pe-3 font-mono">{desktop.desktop_id ?? desktop.id}</td>
                  <td className="py-2.5 pe-3">{desktop.pool_state} · {desktop.status}</td>
                  <td className="py-2.5 pe-3 font-mono">{desktop.workspace_id ?? "—"}</td>
                  <td className="py-2.5 pe-3">{desktop.tunnel_state}</td>
                  <td className="py-2.5 pe-3">{desktop.charge_type ?? "—"} · {desktop.spec ?? "—"}</td>
                  <td className="py-2.5 pe-3">{date(desktop.expires_at)}</td>
                  <td className="py-2.5">
                    <div className="flex gap-1.5">
                      {desktop.pool_state === "assigned" && (
                        <button className={button} disabled={release.isPending} onClick={() => {
                          if (window.confirm(t("desktops.confirmRelease"))) release.mutate(desktop.desktop_id ?? desktop.id)
                        }}>{t("desktops.release")}</button>
                      )}
                      {["reserve", "prewarm", "released"].includes(desktop.pool_state) && (
                        <button className={button} disabled={recycle.isPending} onClick={() => {
                          if (window.confirm(t("desktops.confirmRecycle"))) recycle.mutate(desktop.desktop_id ?? desktop.id)
                        }}>{t("desktops.recycle")}</button>
                      )}
                      {["reserve", "prewarm", "released"].includes(desktop.pool_state) && (
                        <button className={button} disabled={retire.isPending} onClick={() => {
                          if (window.confirm(t("desktops.confirmRetire"))) retire.mutate(desktop.desktop_id ?? desktop.id)
                        }}>{t("desktops.retire")}</button>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <section className={card}>
        <div className="flex flex-wrap items-baseline justify-between gap-3">
          <h2 className="text-base font-medium">{t("alerts.title")}</h2>
          <span className="text-xs text-n500">
            {t("snapshot", {
              time: date(snapshot.data!.taken_at),
              sources: snapshot.data!.sources.map((source) => `${source.source}:${source.ok ? "ok" : "error"}`).join(" · "),
            })}
          </span>
        </div>
        <div className="mt-3 flex flex-col gap-2">
          {alerts.data!.items.length === 0 && <p className="text-sm text-n500">{t("alerts.empty")}</p>}
          {alerts.data!.items.map((alert) => (
            <div key={alert.id} className="flex flex-wrap items-center gap-2 rounded-lg bg-bg px-3 py-2 text-xs">
              <span className={alert.severity === "critical" ? "text-danger" : "text-n700"}>{alert.severity}</span>
              <span className="font-medium">{alert.rule}</span>
              <span className="font-mono text-n500">{alert.resource_id}</span>
              <span className="min-w-48 flex-1 text-n700">{alert.message}</span>
              {!alert.acked_at && <button className={button} onClick={() => ack.mutate(alert.id)}>{t("alerts.ack")}</button>}
              <button className={button} onClick={() => mute.mutate({
                id: alert.id,
                until: new Date(Date.now() + 24 * 60 * 60 * 1000).toISOString(),
              })}>{t("alerts.mute")}</button>
            </div>
          ))}
        </div>
      </section>
    </div>
  )
}
