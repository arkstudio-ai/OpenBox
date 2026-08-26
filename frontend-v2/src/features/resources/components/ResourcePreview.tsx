// The preview surface. Pictures and video play inline straight off the
// presigned URL; text and code come back through the API (the bucket refuses
// a cross-origin read); everything else gets an honest "no preview" card
// rather than a broken embed.
import { useTranslation } from "react-i18next"
import { Download } from "lucide-react"
import { Spinner } from "@/shared/ui/Spinner"
import { KIND_ICON } from "../constants"
import { useResourceText } from "../api/preview"
import type { Resource } from "../types"

interface Props {
  resource: Resource
  onDownload: () => void
}

const TEXT_KINDS = new Set(["code", "document"])
const TEXT_MIME = /^(text\/|application\/(json|xml|x-yaml|javascript))/

function isTextPreviewable(resource: Resource): boolean {
  if (resource.kind === "code") return true
  if (!TEXT_KINDS.has(resource.kind)) return false
  return TEXT_MIME.test(resource.mime) || /\.(md|txt|csv|log)$/i.test(resource.name)
}

function isPdf(resource: Resource): boolean {
  return resource.mime === "application/pdf" || /\.pdf$/i.test(resource.name)
}

function TextBody({ resource }: { resource: Resource }) {
  const { t } = useTranslation("resources")
  const text = useResourceText(resource.id, true)

  if (text.isLoading) {
    return (
      <div className="flex flex-1 items-center justify-center">
        <Spinner className="size-4" />
      </div>
    )
  }
  if (text.isError || !text.data) {
    return <span className="text-md text-n600 m-auto">{t("preview.textFailed")}</span>
  }
  return (
    <pre className="scr border-hair bg-card text-ink m-auto w-full max-w-3xl overflow-auto rounded-xl border p-4 text-xs leading-relaxed whitespace-pre-wrap">
      {text.data.text}
    </pre>
  )
}

function Unsupported({ resource, onDownload }: Props) {
  const { t } = useTranslation("resources")
  const Icon = KIND_ICON[resource.kind]
  return (
    <div className="m-auto flex flex-col items-center gap-3 text-center">
      <Icon className="text-n500 size-9" strokeWidth={1.4} />
      <span className="text-md text-n700">{t("preview.unsupported")}</span>
      <button
        type="button"
        onClick={onDownload}
        className="bg-ink text-bg flex items-center gap-2 rounded-full px-4 py-2 text-xs font-medium"
      >
        <Download className="size-3.5" strokeWidth={2.2} />
        {t("actions.download")}
      </button>
    </div>
  )
}

export function ResourcePreview({ resource, onDownload }: Props) {
  return (
    <div className="scr flex min-h-0 flex-1 flex-col overflow-auto p-6">
      {resource.kind === "image" ? (
        <img
          src={resource.url}
          alt={resource.name}
          className="m-auto max-h-full max-w-full rounded-xl object-contain"
        />
      ) : resource.kind === "video" ? (
        <video src={resource.url} controls className="m-auto max-h-full max-w-full rounded-xl" />
      ) : resource.kind === "audio" ? (
        <audio src={resource.url} controls className="m-auto w-full max-w-lg" />
      ) : isPdf(resource) ? (
        <iframe
          src={resource.url}
          title={resource.name}
          className="border-hair min-h-150 flex-1 rounded-xl border"
        />
      ) : isTextPreviewable(resource) ? (
        <TextBody resource={resource} />
      ) : (
        <Unsupported resource={resource} onDownload={onDownload} />
      )}
    </div>
  )
}
