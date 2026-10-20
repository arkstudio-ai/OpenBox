// Right column: the meta header DEEIX puts above its preview (thumbnail,
// name, date | type | size, status chip) plus the preview surface itself.
import { useTranslation } from "react-i18next"
import { useState } from "react"
import { Download, ExternalLink, Trash2 } from "lucide-react"
import { formatBytes, formatDateTime } from "@/shared/lib/format"
import { cn } from "@/shared/lib/cn"
import { downloadResource, useDescribeResource, useRecognizeResource } from "../api/resources"
import { KIND_ICON, SOURCE_LABEL } from "../constants"
import { ResourcePreview } from "./ResourcePreview"
import type { Resource } from "../types"

interface Props {
  resource: Resource | null
  onDelete: (resource: Resource) => void
}

const ACTION = "flex size-7 items-center justify-center rounded-full text-n700 hover:bg-hairsoft"

export function ResourceDetail({ resource, onDelete }: Props) {
  const { t } = useTranslation("resources")

  if (!resource) {
    return (
      <section className="flex min-w-0 flex-1 items-center justify-center">
        <span className="text-md text-n600">{t("detail.empty")}</span>
      </section>
    )
  }

  const Icon = KIND_ICON[resource.kind]
  const typeLabel =
    resource.mime && resource.mime !== "application/octet-stream" ? resource.mime : resource.kind
  const download = () => void downloadResource(resource.id)

  return (
    <section className="flex min-w-0 flex-1 flex-col">
      <div className="border-hair flex h-15 flex-none items-center gap-3 border-b px-5">
        <span className="bg-n200 flex size-8 flex-none items-center justify-center overflow-hidden rounded-lg">
          {resource.kind === "image" ? (
            <img src={resource.url} alt="" className="size-8 object-cover" />
          ) : (
            <Icon className="text-n700 size-4.5" strokeWidth={1.9} />
          )}
        </span>
        <div className="flex min-w-0 flex-1 flex-col">
          <span className="text-md text-ink truncate font-medium">{resource.name}</span>
          <span className="text-2xs text-n600 flex min-w-0 items-center gap-1.5 pt-0.5">
            <span className="truncate">
              {formatDateTime(resource.createdAt)}
              <span className="text-n400 px-1.5">|</span>
              {typeLabel}
              <span className="text-n400 px-1.5">|</span>
              {formatBytes(resource.size)}
            </span>
            <span
              className={cn(
                "text-2xs flex-none rounded-md px-1.5 py-0.5 font-medium",
                // a700 is the one accent token redefined per colour mode, so
                // this stays legible in dark as well as light.
                resource.source === "agent" ? "bg-n200 text-a700" : "bg-n200 text-n700",
              )}
            >
              {t(SOURCE_LABEL[resource.source])}
            </span>
          </span>
        </div>
        <button
          type="button"
          onClick={() => window.open(resource.url, "_blank")}
          className={ACTION}
          aria-label={t("actions.open")}
          title={t("actions.open")}
        >
          <ExternalLink className="size-4" strokeWidth={1.9} />
        </button>
        <button
          type="button"
          onClick={download}
          className={ACTION}
          aria-label={t("actions.download")}
          title={t("actions.download")}
        >
          <Download className="size-4" strokeWidth={1.9} />
        </button>
        <button
          type="button"
          onClick={() => onDelete(resource)}
          className={cn(ACTION, "hover:text-dangerink")}
          aria-label={t("actions.delete")}
          title={t("actions.delete")}
        >
          <Trash2 className="size-4" strokeWidth={1.9} />
        </button>
      </div>

      <ResourceDescription key={resource.id} resource={resource} />
      <ResourcePreview resource={resource} onDownload={download} />
    </section>
  )
}

function ResourceDescription({ resource }: { resource: Resource }) {
  const { t } = useTranslation("resources")
  const [draft, setDescription] = useState<string | null>(null)
  const description = draft ?? resource.description ?? ""
  const update = useDescribeResource()
  const recognize = useRecognizeResource()
  const recognizing = resource.descriptionState === "pending" || resource.descriptionState === "processing"
  const changed = description.trim() !== (resource.description ?? "")

  return (
    <form
      className="border-hair flex flex-none flex-col gap-2 border-b px-5 py-3"
      onSubmit={(event) => {
        event.preventDefault()
        if (changed) update.mutate({ id: resource.id, description: description.trim() }, {
          onSuccess: () => setDescription((current) => current === description ? null : current),
        })
      }}
    >
      <label htmlFor={`resource-description-${resource.id}`} className="text-n700 text-sm font-medium">
        {t("description.label")}
      </label>
      <textarea
        id={`resource-description-${resource.id}`}
        value={description}
        onChange={(event) => setDescription(event.target.value)}
        placeholder={t("description.placeholder")}
        maxLength={2000}
        rows={2}
        className="border-hair text-ink bg-surface rounded-lg border px-3 py-2 text-sm"
      />
      <div className="flex items-center justify-between gap-3">
        <span className="text-n600 text-xs">{t("description.hint")}</span>
        <button
          type="submit"
          disabled={!changed || update.isPending}
          className="text-a700 flex-none rounded-md px-3 py-1 text-sm disabled:opacity-40"
        >
          {t(update.isPending ? "description.saving" : "description.save")}
        </button>
      </div>
      {resource.kind === "image" && (
        <div className="text-n600 flex items-center justify-between gap-3 text-xs" aria-live="polite">
          <span>{t(recognizing ? "description.recognizing" : resource.descriptionState === "ready"
            ? "description.recognized" : resource.descriptionState === "failed"
              ? "description.recognitionFailed" : resource.descriptionState === "unsupported"
                ? "description.unsupported" : "description.autoHint")}</span>
          {!resource.description && !recognizing && resource.descriptionState !== "unsupported" && (
            <button type="button" disabled={recognize.isPending || !!draft?.trim()}
              className="text-a700 flex-none rounded-md px-3 py-1 disabled:opacity-40"
              onClick={() => recognize.mutate(resource.id)}>{t("description.recognize")}</button>
          )}
        </div>
      )}
      {recognize.isError && <p role="alert" className="text-dangerink text-xs">{t("description.recognitionFailed")}</p>}
      {update.isError && (
        <p role="alert" className="text-dangerink text-xs">
          {t("description.error")}
        </p>
      )}
    </form>
  )
}
