import { useEffect, useRef, useState } from "react"
import { useTranslation } from "react-i18next"
import { Check, FileText, Layers, Upload, X } from "lucide-react"
import { useAssetUrl } from "@/shared/api/assets"
import { formatBytes } from "@/shared/lib/format"
import { toast } from "@/shared/ui/Toast"
import { useAttachments } from "../hooks/useAttachments"
import type { MentionScope } from "../hooks/useMentionMenu"
import { AttachmentRow } from "./composer/AttachmentRow"
import { MentionScopeBar } from "./composer/MentionScopeBar"

function SelectedAsset({ id, disabled, onRemove }: {
  id: string; disabled: boolean; onRemove: () => void
}) {
  const { t } = useTranslation("chat")
  const asset = useAssetUrl(id)
  return (
    <div className="border-hair flex items-center gap-2 rounded-lg border p-2">
      {asset.data?.mime.startsWith("image/") ? (
        <img src={asset.data.url} alt="" className="size-10 rounded object-cover" />
      ) : <FileText className="text-n500 size-5 shrink-0" aria-hidden />}
      <span className="text-ink min-w-0 flex-1 truncate text-xs">
        {asset.data?.name ?? t(asset.isError ? "question.resourceUnavailable" : "question.resourceLoading")}
      </span>
      <button type="button" disabled={disabled} onClick={onRemove}
        aria-label={t("question.removeResource", { name: asset.data?.name ?? "" })}
        className="text-n600 hover:bg-hairsoft flex size-8 shrink-0 items-center justify-center rounded-full disabled:opacity-40">
        <X className="size-4" aria-hidden />
      </button>
    </div>
  )
}

interface Props {
  sessionId: string
  selected: string[]
  disabled: boolean
  scope?: MentionScope
  onChange: (ids: string[]) => void
  onTransferBlocked: (blocked: boolean) => void
  capacity: number
}

const MAX_UPLOAD = 1024 * 1024 * 1024

/** Completed uploads and library picks share the same durable asset IDs. */
export function QuestionResources({ sessionId, selected, disabled, scope, onChange, onTransferBlocked, capacity }: Props) {
  const { t } = useTranslation("chat")
  // Ask answers require durable asset IDs; a sandbox-only path cannot survive
  // a refreshed question or a worker change. Reuse the composer's OSS path.
  const uploads = useAttachments(null, sessionId)
  const { items: uploadItems, remove: removeUpload } = uploads
  const fileRef = useRef<HTMLInputElement>(null)
  const [browsing, setBrowsing] = useState(false)
  const [query, setQuery] = useState("")
  const blocked = uploads.items.some((item) => item.status !== "done")
  useEffect(() => { onTransferBlocked(blocked) }, [blocked, onTransferBlocked])
  useEffect(() => () => onTransferBlocked(false), [onTransferBlocked])
  useEffect(() => {
    const completed = uploadItems.filter((item) => item.status === "done" && item.assetId)
    if (!completed.length) return
    onChange([...new Set([...selected, ...completed.map((item) => item.assetId!)])])
    for (const item of completed) removeUpload(item.id)
  }, [uploadItems, removeUpload, selected, onChange])

  const pickFiles = (files: File[]) => {
    if (disabled) return
    const accepted = files.filter((file) => {
      if (file.size <= MAX_UPLOAD) return true
      toast("error", t("attachTooLarge"))
      return false
    })
    if (selected.length + uploads.items.length + accepted.length > capacity) {
      toast("error", t("question.resourceLimit"))
      return
    }
    uploads.addFiles(accepted)
  }

  const toggleResource = (id: string) => {
    if (selected.includes(id)) onChange(selected.filter((value) => value !== id))
    else if (selected.length + uploads.items.length >= capacity) toast("error", t("question.resourceLimit"))
    else onChange([...selected, id])
  }

  const resources = scope?.items.filter((item) => item.name.toLowerCase().includes(query.trim().toLowerCase())) ?? []
  return (
    <div className="border-hair mt-2 space-y-2 rounded-xl border p-3">
      <p className="text-n600 text-xs">{t("question.resourcesHint")}</p>
      <div className="flex flex-wrap gap-2">
        {scope && (
          <button type="button" disabled={disabled} aria-expanded={browsing}
            onClick={() => setBrowsing((value) => !value)}
            className="border-hair hover:bg-hairsoft text-ink flex items-center gap-1.5 rounded-full border px-3 py-1.5 text-xs disabled:opacity-40">
            <Layers className="size-3.5" aria-hidden />{t("composer.resourceCenter")}
          </button>
        )}
        <button type="button" disabled={disabled} onClick={() => fileRef.current?.click()}
          className="border-hair hover:bg-hairsoft text-ink flex items-center gap-1.5 rounded-full border px-3 py-1.5 text-xs disabled:opacity-40">
          <Upload className="size-3.5" aria-hidden />{t("composer.uploadFile")}
        </button>
        <input ref={fileRef} type="file" multiple hidden disabled={disabled}
          aria-label={t("question.uploadResources")}
          onChange={(event) => {
            pickFiles([...event.target.files ?? []])
            event.target.value = ""
          }} />
      </div>
      {browsing && scope && (
        <div className="border-hair rounded-lg border p-2">
          <MentionScopeBar scope={scope} />
          <input type="search" value={query} onChange={(event) => setQuery(event.target.value)}
            aria-label={t("question.searchResources")} placeholder={t("question.searchResources")}
            className="border-hair bg-bg text-ink my-2 w-full rounded-lg border px-2 py-1.5 text-xs outline-none" />
          <div className="scr max-h-52 space-y-1 overflow-auto">
            {scope.loading ? <p className="text-n600 p-2 text-xs">{t("composer.mention.loading")}</p>
              : resources.length === 0 ? <p className="text-n600 p-2 text-xs">{t("composer.mention.empty")}</p>
                : resources.map((resource) => (
                  <button key={resource.id} type="button" disabled={disabled}
                    aria-pressed={selected.includes(resource.id)} onClick={() => toggleResource(resource.id)}
                    className="hover:bg-hairsoft text-ink flex w-full items-center gap-2 rounded-lg p-2 text-start text-xs">
                    {resource.kind === "image" ? <img src={resource.url} alt="" loading="lazy" className="size-8 rounded object-cover" />
                      : <FileText className="text-n500 size-5" aria-hidden />}
                    <span className="min-w-0 flex-1 truncate">{resource.name}</span>
                    <span className="text-n600 shrink-0">{formatBytes(resource.size)}</span>
                    {selected.includes(resource.id) && <Check className="text-accent size-4 shrink-0" aria-hidden />}
                  </button>
                ))}
          </div>
        </div>
      )}
      {selected.length > 0 && <div className="grid gap-2 sm:grid-cols-2">
        {selected.map((id) => <SelectedAsset key={id} id={id} disabled={disabled}
          onRemove={() => onChange(selected.filter((value) => value !== id))} />)}
      </div>}
      <AttachmentRow items={uploads.items} onRemove={uploads.remove} />
      {blocked && <p className="text-n600 text-xs" role="status">
        {t(uploads.uploading ? "question.resourcesUploading" : "question.resourcesFailed")}
      </p>}
    </div>
  )
}
