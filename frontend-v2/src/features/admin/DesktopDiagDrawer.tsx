import { useEffect, useState } from "react"
import { createPortal } from "react-dom"
import { useTranslation } from "react-i18next"
import { cn } from "@/shared/lib/cn"
import { Spinner } from "@/shared/ui/Spinner"
import { useCollectDiag, useDesktopDiags, useDesktopEvents, useDiag } from "./api"
import type { DesktopEvent, DiagCollectResult, DiagLight, DiagRecord, DiagReport } from "./types"

const LIGHTS = ["chrome", "relay", "x", "unit", "runtime"] as const
const button = "rounded-full border border-hair px-3 py-1.5 text-xs text-n800 hover:bg-hairsoft disabled:opacity-50"

function time(value?: string | null) {
  return value ? new Date(value).toLocaleString() : "—"
}

function lightClass(light: DiagLight | undefined) {
  switch (light) {
    case "ok":
      return "border-hair text-n800"
    case "degraded":
      return "border-hair bg-hairsoft text-n800"
    case "down":
      return "border-danger/40 bg-danger/10 text-danger"
    default:
      return "border-hair text-n500"
  }
}

function statusClass(status: string) {
  if (status === "fail" || status === "timeout") return "text-danger"
  if (status === "info") return "text-n500"
  return "text-n700"
}

/** Five lights: the collector's verdict per layer. */
function Lights({ lights }: { lights: Record<string, DiagLight> }) {
  const { t } = useTranslation("admin")
  return (
    <div className="flex flex-wrap gap-2" data-testid="diag-lights">
      {LIGHTS.map((name) => (
        <span
          key={name}
          className={cn("rounded-full border px-3 py-1 text-xs", lightClass(lights[name]))}
        >
          {t(`diag.lights.${name}`)} · {t(`diag.light.${lights[name] ?? "unknown"}`)}
        </span>
      ))}
    </div>
  )
}

function LogTail({ title, lines }: { title: string; lines?: string[] | null }) {
  const { t } = useTranslation("admin")
  return (
    <details className="rounded-lg bg-bg px-3 py-2">
      <summary className="cursor-pointer text-xs text-n700">{title}</summary>
      <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap break-all font-mono text-[11px] leading-snug text-n700">
        {lines && lines.length > 0 ? lines.join("\n") : t("diag.empty")}
      </pre>
    </details>
  )
}

/** Findings and per-section collection errors of one report. */
function Findings({ report }: { report: DiagReport }) {
  const { t } = useTranslation("admin")
  const findings = report.summary?.findings ?? []
  return (
    <div className="rounded-lg bg-bg px-3 py-2 text-xs">
      <div className="text-n500">{t("diag.findings")}</div>
      {findings.length === 0 ? (
        <p className="mt-1 text-n700">{t("diag.noFindings")}</p>
      ) : (
        <ul className="mt-1 flex list-disc flex-col gap-0.5 ps-4 text-n800">
          {findings.map((finding) => <li key={finding}>{finding}</li>)}
        </ul>
      )}
      {report.errors.length > 0 && (
        <p className="mt-1 text-n500">
          {t("diag.sections", { sections: report.errors.map((e) => `${e.section}: ${e.error}`).join(" · ") })}
        </p>
      )}
    </div>
  )
}

/** What one snapshot saw: lights, findings, log tails. */
function Snapshot({ report }: { report: DiagReport }) {
  const { t } = useTranslation("admin")
  return (
    <>
      {report.summary?.lights && <Lights lights={report.summary.lights} />}
      <Findings report={report} />
      <div className="flex flex-col gap-1.5">
        <div className="text-xs text-n500">{t("diag.logs")}</div>
        <LogTail title={t("diag.chromeLog")} lines={report.chrome?.log?.lines} />
        <LogTail title={t("diag.relayLog")} lines={report.relay?.log?.lines} />
        <LogTail title={t("diag.journal")} lines={report.logs?.journal?.lines} />
      </div>
    </>
  )
}

/** Outcome line of a snapshot just taken from this drawer. */
function CollectOutcome({ result, error }: { result?: DiagCollectResult; error?: Error | null }) {
  const { t } = useTranslation("admin")
  if (error) return <p className="text-xs text-danger">{t("diag.collectFailed", { error: error.message })}</p>
  if (!result) return null
  return (
    <p className="text-xs text-n600">
      {t("diag.collectedVia", { via: result.via ?? "?", ms: result.elapsed_ms ?? "?" })}
      {result.fallback_errors.length > 0 && (
        <span className="block text-n500">
          {t("diag.fallback", { error: result.fallback_errors.join("; ") })}
        </span>
      )}
    </p>
  )
}

/** Picker over the desktop's stored snapshots. */
function SnapshotPicker({
  items,
  value,
  onChange,
}: {
  items: DiagRecord[]
  value: string
  onChange: (id: string) => void
}) {
  const { t } = useTranslation("admin")
  if (items.length < 2) return null
  return (
    <select
      className="w-fit rounded-lg border border-hair bg-bg px-2 py-1 text-xs"
      value={value}
      onChange={(event) => onChange(event.target.value)}
      aria-label={t("diag.pick")}
    >
      {items.map((item) => (
        <option key={item.id} value={item.id}>
          {time(item.ts)} · {item.reason} · {item.collected ? "✓" : "✗"}
        </option>
      ))}
    </select>
  )
}

/** One timeline row; its correlation ids unfold on click. */
function EventRow({ event }: { event: DesktopEvent }) {
  const { t } = useTranslation("admin")
  const [open, setOpen] = useState(false)
  const ids: Array<[string, string | null | undefined]> = [
    [t("diag.fields.session"), event.session_id],
    [t("diag.fields.toolCall"), event.tool_call_id],
    [t("diag.fields.request"), event.request_id],
    [t("diag.fields.event"), event.id],
  ]
  return (
    <li className="rounded-lg bg-bg px-3 py-2 text-xs">
      <button
        type="button"
        className="flex w-full flex-wrap items-baseline gap-x-2 gap-y-1 text-start"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
      >
        <span className="font-mono text-n500">{time(event.ts)}</span>
        <span className="font-medium">{t(`diag.kinds.${event.kind}`, { defaultValue: event.kind })}</span>
        <span className={statusClass(event.status)}>{event.status}</span>
        {event.duration_ms != null && (
          <span className="text-n500">{t("diag.duration", { ms: event.duration_ms })}</span>
        )}
        <span className="min-w-48 flex-1 text-n700">{event.summary}</span>
        {event.diag_id && (
          <span className="font-mono text-n500">{t("diag.cites", { id: event.diag_id })}</span>
        )}
      </button>
      {open && (
        <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 font-mono text-[11px] text-n600">
          {ids.filter(([, value]) => value).map(([label, value]) => (
            <div key={label} className="contents">
              <dt>{label}</dt>
              <dd className="break-all">{value}</dd>
            </div>
          ))}
        </dl>
      )}
    </li>
  )
}

function Timeline({ items }: { items: DesktopEvent[] }) {
  const { t } = useTranslation("admin")
  return (
    <section className="flex flex-col gap-2">
      <h3 className="text-sm font-medium">{t("diag.timeline")}</h3>
      {items.length === 0 ? (
        <p className="text-sm text-n500">{t("diag.timelineEmpty")}</p>
      ) : (
        <ul className="flex flex-col gap-1.5">
          {items.map((event) => <EventRow key={event.id} event={event} />)}
        </ul>
      )}
    </section>
  )
}

/** The snapshot section: a fresh capture wins over the stored one it replaces. */
function SnapshotSection({
  desktopId,
  fresh,
  stored,
}: {
  desktopId: string
  fresh?: DiagCollectResult
  stored: DiagRecord[]
}) {
  const { t } = useTranslation("admin")
  const [picked, setPicked] = useState<string | null>(null)
  const storedId = picked ?? stored[0]?.id ?? null
  const latest = useDiag(fresh ? null : storedId)

  const report = fresh ?? latest.data?.report
  const meta = fresh
    ? { time: fresh.collected_at, reason: `${t("diag.freshReason")} · ${fresh.via ?? ""}` }
    : latest.data
      ? { time: latest.data.ts, reason: latest.data.reason ?? "" }
      : null

  return (
    <section className="flex flex-col gap-2" data-desktop={desktopId}>
      {meta ? (
        <p className="text-xs text-n500">{t("diag.latest", { time: time(meta.time), reason: meta.reason })}</p>
      ) : (
        !latest.isPending || !storedId ? <p className="text-sm text-n500">{t("diag.noSnapshot")}</p> : null
      )}
      {latest.isPending && storedId && !fresh && (
        <div className="flex justify-center py-2"><Spinner className="size-4" /></div>
      )}
      {report && <Snapshot report={report} />}
      {!fresh && <SnapshotPicker items={stored} value={storedId ?? ""} onChange={setPicked} />}
    </section>
  )
}

interface DrawerProps {
  desktopId: string
  onClose: () => void
}

/**
 * Everything an operator needs to read a broken browser without logging into
 * the desktop: the five lights and findings from the latest snapshot, a
 * button for a fresh one, the log tails it carried, and the desktop's
 * timeline of bring-ups, launches, repairs, verifies and leases.
 */
export function DesktopDiagDrawer({ desktopId, onClose }: DrawerProps) {
  const { t } = useTranslation("admin")
  const events = useDesktopEvents(desktopId)
  const diags = useDesktopDiags(desktopId)
  const collect = useCollectDiag()

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose()
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [onClose])

  let body
  if (events.isPending || diags.isPending) {
    body = <div className="flex justify-center py-8"><Spinner className="size-5" /></div>
  } else if (events.isError || diags.isError) {
    body = <p className="text-sm text-danger">{t("diag.loadFailed")}</p>
  } else {
    body = (
      <>
        <SnapshotSection desktopId={desktopId} fresh={collect.data} stored={diags.data.items} />
        <Timeline items={events.data.items} />
      </>
    )
  }

  return createPortal(
    <div className="fixed inset-0 z-50 flex justify-end bg-n900/30" onClick={onClose} role="presentation">
      <aside
        className="flex h-full w-[min(48rem,100vw)] flex-col gap-4 overflow-y-auto border-s border-hair bg-card p-5 shadow-pop"
        role="dialog"
        aria-modal="true"
        aria-label={t("diag.title")}
        onClick={(event) => event.stopPropagation()}
      >
        <header className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <h2 className="text-base font-medium">{t("diag.title")}</h2>
            <p className="font-mono text-xs text-n500">{desktopId}</p>
          </div>
          <div className="flex gap-2">
            <button className={button} disabled={collect.isPending} onClick={() => collect.mutate({ desktopId })}>
              {collect.isPending ? t("diag.collecting") : t("diag.collect")}
            </button>
            <button className={button} onClick={onClose}>{t("diag.close")}</button>
          </div>
        </header>
        <CollectOutcome result={collect.data} error={collect.error as Error | null} />
        {body}
      </aside>
    </div>,
    document.body,
  )
}
