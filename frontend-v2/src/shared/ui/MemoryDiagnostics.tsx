import { useTranslation } from "react-i18next"
import { StatusPill, type StatusTone } from "@/shared/ui/StatusPill"

export const memoryButton =
  "border-hair hover:bg-hairsoft min-h-9 rounded-full border px-3.5 py-1.5 text-sm disabled:cursor-not-allowed disabled:opacity-50"
export const memoryPrimary =
  "bg-ink text-card hover:opacity-90 min-h-9 rounded-full px-4 py-1.5 text-sm font-medium disabled:cursor-not-allowed disabled:opacity-50"
export const memoryInput =
  "border-hair bg-card text-ink min-h-10 min-w-0 rounded-lg border px-3 py-2 text-sm outline-none focus:border-accent"
export const memoryCard = "border-hair bg-card rounded-xl border p-4"

export function MemoryStatus({ status }: { status: string }) {
  const { t } = useTranslation("memory")
  const raw = status.toLowerCase()
  const key =
    (
      {
        succeeded: "completed",
        success: "completed",
        error: "failed",
        in_progress: "running",
        canceled: "cancelled",
      } as Record<string, string>
    )[raw] ?? raw
  const tone: StatusTone = [
    "active",
    "confirmed",
    "completed",
    "cleaned",
    "published",
    "healthy",
    "approved",
    "unchanged",
  ].includes(key)
    ? "ok"
    : ["failed", "rejected"].includes(key)
      ? "danger"
      : [
            "candidate",
            "pending",
            "stale",
            "degraded",
            "running",
            "queued",
            "retry",
            "stopped_cleanup_pending",
          ].includes(key)
        ? "warn"
        : "muted"
  return <StatusPill tone={tone}>{t(`status.${key}`, { defaultValue: status })}</StatusPill>
}

/** Only renders bounded, authorized diagnostics already supplied by the server. */
export function DiagnosticData({ data, label }: { data: unknown; label?: string }) {
  const { t } = useTranslation("memory")
  if (data === undefined || data === null) return <p className="text-n500 text-sm">{t("unknown")}</p>
  return (
    <pre
      aria-label={label}
      className="bg-hairsoft text-n700 max-h-80 overflow-auto rounded-lg p-3 text-xs leading-relaxed break-words whitespace-pre-wrap"
    >
      {typeof data === "string" ? data : JSON.stringify(data, null, 2)}
    </pre>
  )
}
