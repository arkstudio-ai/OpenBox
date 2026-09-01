import { useCallback, useEffect, useRef, useState } from "react"
import { useTranslation } from "react-i18next"
import { ExternalLink, Radio, RefreshCw } from "lucide-react"
import { useCreateContainer, useListeningPorts, useRunningContainer } from "../api/containers"
import { authorizePreviewNavigation } from "../lib/preview-access"
import { containersApi } from "@/shared/api/containers"
import { cn } from "@/shared/lib/cn"
import { Spinner } from "@/shared/ui/Spinner"
import { EmptyState } from "./EmptyState"

interface PreviewSurfaceProps {
  containerId: string
}

function parsePort(value: string): number | null {
  if (!/^\d+$/.test(value)) return null
  const port = Number(value)
  return Number.isInteger(port) && port >= 1 && port <= 65535 ? port : null
}

function PreviewSurface({ containerId }: PreviewSurfaceProps) {
  const { t } = useTranslation("workbench")
  const ports = useListeningPorts(containerId)
  const [input, setInput] = useState("")
  const [activePort, setActivePort] = useState<number | null>(null)
  const [previewUrl, setPreviewUrl] = useState<string | null>(null)
  const [isolatedPreview, setIsolatedPreview] = useState(false)
  const [frameKey, setFrameKey] = useState(0)
  const [frameLoading, setFrameLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [authorizing, setAuthorizing] = useState(false)
  const requestSeq = useRef(0)
  const mounted = useRef(true)

  useEffect(
    () => () => {
      mounted.current = false
      requestSeq.current += 1
    },
    [],
  )

  const openPort = useCallback(
    async (port: number) => {
      const seq = requestSeq.current + 1
      requestSeq.current = seq
      setInput(String(port))
      setError(null)
      setAuthorizing(true)
      try {
        await authorizePreviewNavigation(containersApi.requestPreviewAccess, containerId, port, (preview) => {
          if (requestSeq.current !== seq) return
          setActivePort(port)
          setPreviewUrl(preview.url)
          setIsolatedPreview(preview.isolated)
          setFrameLoading(true)
          setFrameKey((key) => key + 1)
        })
      } catch {
        if (requestSeq.current === seq) setError(t("preview.authorizeFailed"))
      } finally {
        if (requestSeq.current === seq) setAuthorizing(false)
      }
    },
    [containerId, t],
  )

  const submitPort = () => {
    if (authorizing) return
    const port = parsePort(input.trim())
    if (port === null) {
      setError(t("preview.invalidPort"))
      return
    }
    void openPort(port)
  }

  const openExternal = () => {
    if (activePort === null || !isolatedPreview || authorizing) return
    const popup = window.open("about:blank", "_blank")
    if (!popup) {
      setError(t("preview.popupBlocked"))
      return
    }
    popup.opener = null
    setError(null)
    setAuthorizing(true)
    void authorizePreviewNavigation(
      containersApi.requestPreviewAccess,
      containerId,
      activePort,
      (preview) => {
        if (!preview.isolated || !mounted.current) throw new Error("preview_origin_required")
        popup.location.replace(preview.url)
      },
    )
      .catch(() => {
        popup.close()
        if (mounted.current) setError(t("preview.authorizeFailed"))
      })
      .finally(() => {
        if (mounted.current) setAuthorizing(false)
      })
  }

  const detected = ports.data?.ports ?? []

  return (
    <div className="flex min-h-0 flex-1 flex-col px-3 pb-3">
      <div className="flex flex-none flex-wrap items-center gap-2 pb-2.5">
        <label htmlFor="preview-port" className="text-n700 text-xs font-medium">
          {t("preview.portLabel")}
        </label>
        <input
          id="preview-port"
          type="number"
          inputMode="numeric"
          min={1}
          max={65535}
          value={input}
          onChange={(event) => {
            requestSeq.current += 1
            setAuthorizing(false)
            setInput(event.target.value)
          }}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !authorizing) submitPort()
          }}
          placeholder={t("preview.portPlaceholder")}
          className="border-hair bg-card text-n700 focus:border-a500 focus:ring-a200 h-11 w-24 rounded-full border px-3 font-mono text-sm outline-none focus:ring-2"
        />
        <button
          type="button"
          onClick={submitPort}
          disabled={authorizing}
          className="bg-ink text-bg inline-flex h-11 items-center gap-2 rounded-full px-4 text-sm hover:opacity-90 disabled:opacity-60"
        >
          {authorizing && <Spinner className="border-bg/40 border-t-bg size-3.5" />}
          {t("preview.open")}
        </button>
        {activePort !== null && (
          <div className="ms-auto flex items-center gap-1">
            <button
              type="button"
              onClick={() => void openPort(activePort)}
              disabled={authorizing}
              title={t("preview.refresh")}
              aria-label={t("preview.refresh")}
              className="text-n700 hover:bg-hairsoft flex size-11 items-center justify-center rounded-full disabled:opacity-50"
            >
              <RefreshCw size={15} strokeWidth={2.4} />
            </button>
            {isolatedPreview && (
              <button
                type="button"
                onClick={openExternal}
                disabled={authorizing}
                title={t("preview.openExternal")}
                aria-label={t("preview.openExternal")}
                className="text-n700 hover:bg-hairsoft flex size-11 items-center justify-center rounded-full disabled:opacity-50"
              >
                <ExternalLink size={15} strokeWidth={2.4} />
              </button>
            )}
          </div>
        )}
      </div>

      {error && (
        <div role="alert" className="bg-dangersoft text-dangerink mb-2 rounded-xl px-3 py-2 text-sm">
          {error}
        </div>
      )}

      <div className="mb-2 flex min-h-9 flex-none items-center gap-2 overflow-x-auto" aria-live="polite">
        <span className="text-n700 inline-flex flex-none items-center gap-1.5 text-xs">
          <Radio size={13} strokeWidth={2.4} aria-hidden="true" />
          {t("preview.detected")}
        </span>
        {detected.map((item) => (
          <button
            key={item.port}
            type="button"
            onClick={() => void openPort(item.port)}
            disabled={authorizing}
            aria-pressed={activePort === item.port}
            title={item.command || item.process || undefined}
            className={cn(
              "inline-flex h-11 flex-none items-center gap-1.5 rounded-full border px-3 font-mono text-xs disabled:opacity-50",
              activePort === item.port
                ? "border-ink bg-ink text-bg"
                : "border-hair bg-card text-n700 hover:bg-hairsoft",
            )}
          >
            <span className="bg-s500 size-1.5 rounded-full" aria-hidden="true" />
            {item.port}
            {item.process && <span className="max-w-24 truncate opacity-65">{item.process}</span>}
          </button>
        ))}
        {ports.isLoading && <span className="text-n700 text-xs">{t("preview.detecting")}</span>}
        {!ports.isLoading && !ports.isError && detected.length === 0 && (
          <span className="text-n700 text-xs">{t("preview.noPorts")}</span>
        )}
        {ports.isError && (
          <button
            type="button"
            onClick={() => void ports.refetch()}
            className="text-dangerink hover:bg-dangersoft h-11 rounded-full px-3 text-xs"
          >
            {t("preview.loadFailed")} · {t("preview.retry")}
          </button>
        )}
      </div>

      <div
        className="border-hair bg-card relative flex min-h-0 flex-1 overflow-hidden rounded-2xl border"
        aria-busy={frameLoading}
      >
        {previewUrl && activePort !== null ? (
          <>
            <iframe
              key={frameKey}
              src={previewUrl}
              title={t("preview.frameTitle", { port: activePort })}
              sandbox={
                isolatedPreview
                  ? "allow-scripts allow-forms allow-popups allow-same-origin"
                  : "allow-scripts allow-forms allow-popups"
              }
              onLoad={() => setFrameLoading(false)}
              className="size-full border-0 bg-white"
            />
            {frameLoading && (
              <div className="bg-card/90 absolute inset-0 flex items-center justify-center">
                <Spinner className="border-hair border-t-ink size-5" />
              </div>
            )}
          </>
        ) : (
          <EmptyState title={t("preview.empty")} hint={t("preview.emptyHint")} />
        )}
      </div>
    </div>
  )
}

export function PreviewTab() {
  const { t } = useTranslation("workbench")
  const running = useRunningContainer()
  const create = useCreateContainer()

  if (!running) {
    return (
      <EmptyState
        title={t("sandbox.none")}
        action={{
          label: t("sandbox.create"),
          onClick: () => create.mutate(undefined),
          pending: create.isPending,
        }}
      />
    )
  }

  return <PreviewSurface key={running.id} containerId={running.id} />
}
