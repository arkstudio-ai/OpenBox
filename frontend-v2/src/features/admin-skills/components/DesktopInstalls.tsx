import { useState } from "react"
import { useTranslation } from "react-i18next"
import { Pagination } from "@/shared/ui/Pagination"
import { DisplayIcon } from "@/shared/ui/DisplayIcon"
import { formatDateTime } from "@/shared/lib/format"
import { useDesktops, useScanDesktop, useUninstallFromDesktop } from "../api/management"
import type { Desktop, DesktopSkill } from "../types"
import { ListingDialog } from "./ListingDialog"
import { SearchBox } from "./SearchBox"

const BUTTON = "rounded-full border border-hair px-4 py-2 text-sm hover:bg-hairsoft disabled:opacity-40"

export function DesktopInstalls() {
  const { t } = useTranslation("admin-skills")
  const [search, setSearch] = useState("")
  const [offset, setOffset] = useState(0)
  const [target, setTarget] = useState<Desktop | null>(null)
  const [busy, setBusy] = useState(false)
  const query = useDesktops(search, offset)
  const total = query.data?.total ?? 0
  return (
    <section className="border-hair bg-card flex min-w-0 flex-col gap-4 rounded-xl border p-4">
      <p className="text-n600 text-sm">{t("desktop.hint")}</p>
      <SearchBox
        value={search}
        label={t("desktop.search")}
        placeholder={t("desktop.search")}
        onChange={(q) => {
          if (!busy) {
            setSearch(q)
            setOffset(0)
          }
        }}
      />
      {query.isPending && <p role="status">{t("list.loading")}</p>}
      {query.isError && (
        <p role="alert" className="text-danger">
          {t("list.error")}
        </p>
      )}
      {query.data?.items.length === 0 && <p className="text-n600 py-6">{t("desktop.empty")}</p>}
      <div className="grid min-w-0 gap-2 md:grid-cols-2">
        {query.data?.items.map((desktop) => (
          <button
            key={desktop.desktop_id}
            type="button"
            className="border-hair hover:bg-hairsoft min-w-0 rounded-xl border p-3 text-start disabled:opacity-40"
            disabled={busy}
            aria-pressed={target?.desktop_id === desktop.desktop_id}
            onClick={() => setTarget(desktop)}
          >
            <span className="block font-medium break-all">
              {desktop.workspace_name || desktop.username || desktop.desktop_id}
            </span>
            <span className="text-n600 block font-mono text-xs break-all">{desktop.desktop_id}</span>
            <span className="text-n600 text-xs">
              {t("desktop.connection", { status: desktop.status, channel: desktop.channel_state || "—" })}
            </span>
          </button>
        ))}
      </div>
      {total > 20 && (
        <Pagination
          offset={offset}
          total={total}
          limit={20}
          onOffsetChange={(next) => {
            if (!busy) setOffset(next)
          }}
          labels={{
            previous: t("list.previous"),
            next: t("list.next"),
            nav: t("desktop.nav"),
            range: t("list.range", { from: offset + 1, to: Math.min(offset + 20, total), total }),
          }}
        />
      )}
      {target && <DesktopScope key={target.desktop_id} desktop={target} onBusyChange={setBusy} />}
    </section>
  )
}

function DesktopScope({
  desktop,
  onBusyChange,
}: {
  desktop: Desktop
  onBusyChange: (busy: boolean) => void
}) {
  const { t } = useTranslation("admin-skills")
  const [user, setUser] = useState(desktop.members[0]?.id ?? "")
  const [busy, setBusy] = useState(false)
  const pending = (value: boolean) => {
    setBusy(value)
    onBusyChange(value)
  }
  return (
    <div className="border-hair flex min-w-0 flex-col gap-3 border-t pt-4">
      <h3 className="text-sm font-medium break-all">{desktop.desktop_id}</h3>
      <label className="text-sm">
        {t("desktop.user")}
        <select
          value={user}
          disabled={busy}
          onChange={(event) => setUser(event.target.value)}
          className="border-hair bg-bg mt-1 block w-full rounded-lg border p-2 text-sm"
        >
          {desktop.members.map((member) => (
            <option key={member.id} value={member.id}>
              {member.username} · {member.email}
            </option>
          ))}
        </select>
      </label>
      {!user ? (
        <p className="text-n600">{t("desktop.noMember")}</p>
      ) : (
        <ScanPanel key={user} desktop={desktop.desktop_id} user={user} onBusyChange={pending} />
      )}
    </div>
  )
}

function ScanPanel({
  desktop,
  user,
  onBusyChange,
}: {
  desktop: string
  user: string
  onBusyChange: (busy: boolean) => void
}) {
  const { t } = useTranslation("admin-skills")
  const scan = useScanDesktop(desktop, user)
  const uninstall = useUninstallFromDesktop(desktop, user)
  const [removing, setRemoving] = useState<DesktopSkill | null>(null)
  const [message, setMessage] = useState("")
  const busy = scan.isPending || uninstall.isPending
  const refresh = async () => {
    onBusyChange(true)
    try {
      await scan.mutateAsync()
    } catch {
      /* Explicit unknown state below. */
    } finally {
      onBusyChange(false)
    }
  }
  const remove = async (reason: string) => {
    if (!removing || busy) return
    onBusyChange(true)
    setMessage("")
    try {
      await uninstall.mutateAsync({ kind: removing.kind, install_dir: removing.install_dir, reason })
      setRemoving(null)
      setMessage(t("desktop.removed"))
      scan.reset()
      await scan.mutateAsync()
    } catch {
      /* Failure remains visible; never claim an unverified deletion. */
    } finally {
      onBusyChange(false)
    }
  }
  const data = scan.isSuccess ? scan.data : undefined
  return (
    <>
      <button type="button" className={`${BUTTON} self-start`} disabled={busy} onClick={() => void refresh()}>
        {t(scan.isPending ? "desktop.scanning" : "desktop.scan")}
      </button>
      {message && (
        <p role="status" className="text-n700 text-sm">
          {message}
        </p>
      )}
      {scan.isError && (
        <p role="alert" className="text-danger text-sm">
          {t("desktop.unknown")} {scan.error.message}
        </p>
      )}
      {data && (
        <>
          <p className="text-n600 text-xs">
            {t("desktop.scannedAt", { time: formatDateTime(data.scanned_at) })}
          </p>
          {!!data.unavailable.length && (
            <p role="alert" className="text-danger text-sm">
              {t("desktop.partial", { kinds: data.unavailable.join(", ") })}
            </p>
          )}
          {!data.items.length && !data.unavailable.length && <p>{t("desktop.noSkills")}</p>}
          <ul className="space-y-2">
            {data.items.map((item) => (
              <li
                key={`${item.kind}:${item.install_dir}`}
                className="border-hair flex min-w-0 flex-wrap items-start gap-3 rounded-xl border p-3"
              >
                <DisplayIcon icon={item.icon} />
                <div className="min-w-0 flex-1 basis-40">
                  <p className="font-medium break-words">
                    <span>{item.name}</span>{" "}
                    <span className="text-n600 text-xs">{item.kind.toUpperCase()}</span>
                  </p>
                  <p className="text-n600 font-mono text-xs break-all">
                    {item.install_dir} · {item.source}
                  </p>
                  {(item.names?.length ?? 0) > 1 && (
                    <p className="text-n600 text-xs break-words">
                      {t("desktop.collection", { count: item.names!.length, names: item.names!.join(", ") })}
                    </p>
                  )}
                  <p className="text-n600 line-clamp-2 text-xs break-words">{item.description}</p>
                </div>
                <button
                  type="button"
                  className={BUTTON}
                  disabled={!item.removable || busy || uninstall.isError}
                  onClick={() => {
                    uninstall.reset()
                    setRemoving(item)
                  }}
                >
                  {t(item.removable ? "desktop.uninstall" : "desktop.protected")}
                </button>
              </li>
            ))}
          </ul>
        </>
      )}
      {uninstall.isError && !removing && (
        <p role="alert" className="text-danger">
          {t("desktop.removeFailed")}
        </p>
      )}
      <ListingDialog
        open={!!removing}
        title={t("desktop.removeTitle", { name: removing?.name ?? "" })}
        body={t("desktop.removeHint", { desktop, user, dir: removing?.install_dir ?? "" })}
        confirmLabel={t("desktop.uninstall")}
        requireReason
        pending={uninstall.isPending}
        failed={uninstall.isError}
        onConfirm={(reason) => void remove(reason)}
        onClose={() => {
          if (!busy) {
            setRemoving(null)
            uninstall.reset()
            scan.reset()
          }
        }}
      />
    </>
  )
}
