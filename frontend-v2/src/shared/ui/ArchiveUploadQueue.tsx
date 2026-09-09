import { useRef, useState } from "react"
import { useTranslation } from "react-i18next"

export interface ArchiveResult {
  ok: boolean
  error?: string
}
interface Item {
  id: number
  file: File
  status: "queued" | "working" | "success" | "failed"
  error?: string
}
const BUTTON = "rounded-full border border-hair px-3 py-2 text-sm hover:bg-hairsoft disabled:opacity-40"

/** Independent, retryable results. Successful files are never resubmitted. */
export function ArchiveUploadQueue({
  upload,
  onBusyChange,
  accept = ".zip",
  disabled = false,
}: {
  upload: (files: File[]) => Promise<ArchiveResult[]>
  onBusyChange: (busy: boolean) => void
  accept?: string
  disabled?: boolean
}) {
  const { t } = useTranslation("common")
  const input = useRef<HTMLInputElement>(null)
  const active = useRef(false)
  const sequence = useRef(0)
  const [items, setItems] = useState<Item[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")
  const pending = items.filter((item) => item.status === "queued" || item.status === "failed")

  const add = (files: File[]) => {
    const incoming = files.filter(
      (file, index) =>
        !items.some(
          (item) =>
            item.file.name === file.name &&
            item.file.size === file.size &&
            item.file.lastModified === file.lastModified,
        ) &&
        files.findIndex(
          (f) => f.name === file.name && f.size === file.size && f.lastModified === file.lastModified,
        ) === index,
    )
    if (
      items.length + incoming.length > 20 ||
      incoming.some((file) => file.size > 32 * 1024 * 1024) ||
      [...items.map((item) => item.file), ...incoming].reduce((size, file) => size + file.size, 0) >
        128 * 1024 * 1024
    ) {
      setError(t("archiveQueue.limits"))
      return
    }
    setError("")
    setItems((previous) => [
      ...previous,
      ...incoming.map((file) => ({ id: ++sequence.current, file, status: "queued" as const })),
    ])
  }

  const submit = async () => {
    if (active.current || disabled || !pending.length) return
    active.current = true
    setBusy(true)
    onBusyChange(true)
    const ids = new Set(pending.map((item) => item.id))
    setItems((previous) =>
      previous.map((item) => (ids.has(item.id) ? { ...item, status: "working", error: undefined } : item)),
    )
    try {
      const results = await upload(pending.map((item) => item.file))
      setItems((previous) =>
        previous.map((item) => {
          const index = pending.findIndex((p) => p.id === item.id)
          if (index < 0) return item
          const result = results[index]
          return {
            ...item,
            status: result?.ok ? "success" : "failed",
            error: result?.ok ? undefined : result?.error || t("archiveQueue.unknown"),
          }
        }),
      )
    } catch {
      // A broken connection is not proof that nothing was stored. Do not retry automatically.
      setItems((previous) =>
        previous.map((item) =>
          ids.has(item.id) ? { ...item, status: "failed", error: t("archiveQueue.unknown") } : item,
        ),
      )
    } finally {
      active.current = false
      setBusy(false)
      onBusyChange(false)
    }
  }

  return (
    <div className="flex min-w-0 flex-col gap-3">
      <input
        ref={input}
        type="file"
        multiple
        accept={accept}
        aria-label={t("archiveQueue.pick")}
        className="sr-only"
        disabled={busy || disabled}
        onChange={(event) => {
          add(Array.from(event.target.files ?? []))
          event.target.value = ""
        }}
      />
      <button
        type="button"
        className={`${BUTTON} min-h-20 border-dashed`}
        disabled={busy || disabled}
        onClick={() => input.current?.click()}
      >
        {t("archiveQueue.pick")}
      </button>
      <p className="text-n600 text-xs">{t("archiveQueue.limits")}</p>
      {error && (
        <p role="alert" className="text-danger text-sm">
          {error}
        </p>
      )}
      <ul className="max-h-72 space-y-2 overflow-y-auto" aria-live="polite">
        {items.map((item) => (
          <li key={item.id} className="border-hair rounded-lg border p-3 text-sm">
            <div className="flex items-start justify-between gap-2">
              <span className="min-w-0 break-all">{item.file.name}</span>
              <button
                type="button"
                disabled={busy || disabled}
                className="text-n700 shrink-0 underline disabled:opacity-40"
                aria-label={t("archiveQueue.remove", { name: item.file.name })}
                onClick={() => setItems((previous) => previous.filter((p) => p.id !== item.id))}
              >
                ×
              </button>
            </div>
            <p className={item.status === "failed" ? "text-danger" : "text-n600"}>
              {t(`archiveQueue.${item.status}`)}
            </p>
            {item.error && <p className="text-danger text-xs break-words">{item.error}</p>}
          </li>
        ))}
      </ul>
      <button
        type="button"
        className={`${BUTTON} bg-ink text-bg self-end`}
        disabled={busy || disabled || !pending.length}
        onClick={() => void submit()}
      >
        {busy ? t("archiveQueue.working") : t("archiveQueue.submit", { count: pending.length })}
      </button>
    </div>
  )
}
