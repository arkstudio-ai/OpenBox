import { useId, useState } from "react"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { useTranslation } from "react-i18next"
import { Link } from "react-router"
import { Download, FileText, RotateCw, Trash2, UploadCloud } from "lucide-react"
import { useApiErrorMessage } from "@/shared/hooks/useApiErrorMessage"
import { formatBytes, formatSince } from "@/shared/lib/format"
import { paths } from "@/shared/router/paths"
import { toast } from "@/shared/ui/Toast"
import { Dialog, DialogActions, DialogTitle } from "@/shared/ui/Dialog"
import { StatusPill, type StatusTone } from "@/shared/ui/StatusPill"
import { useMemoryScope } from "../api"
import { documentsApi, type KnowledgeDocument } from "../wiki/documents-api"
import { useBackState } from "./back"
import { Highlight } from "./Highlight"
import { button, card, dangerButton, iconButton } from "./ui"

const TONE: Record<string, StatusTone> = {
  ready: "ok",
  searchable: "ok",
  indexing: "muted",
  index_failed: "warn",
  failed: "danger",
}

/** A generous target for dragging files in; clicking it opens the picker. */
export function FileDropzone({
  disabled,
  compact,
  onChoose,
  onDrop,
}: {
  disabled: boolean
  compact?: boolean
  onChoose: () => void
  onDrop: (files: File[]) => void
}) {
  const { t } = useTranslation("knowledge")
  const [over, setOver] = useState(false)
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onChoose}
      onDragOver={(event) => {
        event.preventDefault()
        if (!disabled) setOver(true)
      }}
      onDragLeave={() => setOver(false)}
      onDrop={(event) => {
        event.preventDefault()
        setOver(false)
        if (!disabled) onDrop(Array.from(event.dataTransfer.files))
      }}
      className={
        "flex w-full flex-col items-center justify-center gap-1.5 rounded-2xl border border-dashed text-center transition-colors disabled:cursor-not-allowed disabled:opacity-60 " +
        (compact ? "px-5 py-6 " : "px-6 py-10 ") +
        (over ? "border-accent bg-a100" : "border-n400 bg-card/60 hover:border-accent hover:bg-a100/60")
      }
    >
      <UploadCloud size={compact ? 22 : 28} aria-hidden className="text-a700" />
      <span className="text-ink text-md font-medium">{t(over ? "file.dropActive" : "file.dropTitle")}</span>
      <span className="text-n600 max-w-xl text-xs leading-relaxed">
        {t(disabled ? "file.unavailable" : "file.dropHint")}
      </span>
    </button>
  )
}

export function FileList({
  files,
  projectId,
  query,
}: {
  files: KnowledgeDocument[]
  projectId: string
  query: string
}) {
  const { t } = useTranslation("knowledge")
  const { key } = useMemoryScope()
  const qc = useQueryClient()
  const errorText = useApiErrorMessage()
  const [deleting, setDeleting] = useState<KnowledgeDocument | null>(null)
  const retry = useMutation({
    mutationFn: documentsApi.retry,
    onSuccess: () => void qc.invalidateQueries({ queryKey: [...key, "documents"] }),
    onError: (failure) => toast.error(errorText(failure)),
  })
  const remove = useMutation({
    mutationFn: (file: KnowledgeDocument) => documentsApi.remove(file.id),
    onSuccess: (result, file) => {
      setDeleting(null)
      const pending = result.original_cleanup === "pending"
      toast.success(t(pending ? "file.deletedPending" : "file.deleted", { name: file.filename }))
      // Pages built from the file go too; refresh everything in this scope.
      void qc.invalidateQueries({ queryKey: key })
    },
  })
  return (
    <>
      <ul className={card + " divide-hair divide-y overflow-hidden"}>
        {files.map((file) => (
          <FileRow
            key={file.id}
            file={file}
            projectId={projectId}
            query={query}
            retrying={retry.isPending}
            onRetry={() => retry.mutate(file.id)}
            onDelete={() => {
              remove.reset()
              setDeleting(file)
            }}
          />
        ))}
      </ul>
      {deleting && (
        <Dialog open onClose={() => !remove.isPending && setDeleting(null)} label={t("file.deleteTitle")}>
          <DialogTitle>{t("file.deleteTitle")}</DialogTitle>
          <p className="bg-hairsoft text-ink rounded-xl px-3.5 py-2.5 text-sm break-all">
            {deleting.filename}
          </p>
          <p className="text-n700 text-sm leading-relaxed">{t("file.deleteBody")}</p>
          {remove.error && (
            <p role="alert" className="bg-dangersoft text-dangerink rounded-xl px-3 py-2 text-sm">
              {errorText(remove.error)}
            </p>
          )}
          <DialogActions>
            <button
              type="button"
              className={button}
              disabled={remove.isPending}
              onClick={() => setDeleting(null)}
            >
              {t("file.cancel")}
            </button>
            <button
              type="button"
              className={dangerButton}
              disabled={remove.isPending}
              onClick={() => remove.mutate(deleting)}
            >
              {t(remove.isPending ? "file.deleting" : "file.deleteConfirm")}
            </button>
          </DialogActions>
        </Dialog>
      )}
    </>
  )
}

function FileRow({
  file,
  projectId,
  query,
  retrying,
  onRetry,
  onDelete,
}: {
  file: KnowledgeDocument
  projectId: string
  query: string
  retrying: boolean
  onRetry: () => void
  onDelete: () => void
}) {
  const { t } = useTranslation(["knowledge", "wiki"])
  const errorText = useApiErrorMessage()
  const back = useBackState()
  // Read, download and delete are announced with the file they act on.
  const nameId = useId()
  const failed = ["failed", "index_failed"].includes(file.status)
  const meta = [
    file.bytes ? formatBytes(file.bytes) : null,
    file.created_at ? formatSince(file.created_at) : null,
  ].filter(Boolean)
  return (
    <li className="flex flex-wrap items-center gap-3 px-4 py-3 sm:flex-nowrap sm:px-5">
      <span className="bg-hairsoft text-n700 flex size-9 flex-none items-center justify-center rounded-xl">
        <FileText size={17} aria-hidden />
      </span>
      {/* On a phone the name takes the row beside its icon and the actions
          wrap underneath, rather than squeezing it to a few characters. */}
      <div className="min-w-0 grow basis-[calc(100%-3rem)] sm:basis-0">
        <p
          id={nameId}
          className="text-ink text-md line-clamp-2 break-all sm:line-clamp-1"
          title={file.filename}
        >
          <Highlight text={file.filename} query={query} />
        </p>
        <div className="text-n600 mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs">
          <StatusPill tone={TONE[file.status] ?? "warn"}>
            {t(`file.status.${file.status}`, { defaultValue: t("file.status.pending") })}
          </StatusPill>
          {meta.map((item) => (
            <span key={item}>{item}</span>
          ))}
        </div>
        {file.reason_code && (
          <p className="text-dangerink mt-1 text-xs">
            {t(`documents.errors.${file.reason_code}`, {
              ns: "wiki",
              defaultValue: t("documents.failedHint", { ns: "wiki" }),
            })}
          </p>
        )}
      </div>
      <div className="ms-12 flex flex-none items-center gap-1 sm:ms-0">
        {file.page_ids.length > 0 && (
          <Link
            className={button}
            to={paths.wikiPage(file.page_ids[0], projectId)}
            state={back}
            aria-describedby={nameId}
          >
            {t("file.read")}
          </Link>
        )}
        {failed && (
          <button
            type="button"
            className={button}
            disabled={retrying}
            onClick={onRetry}
            aria-describedby={nameId}
          >
            <RotateCw size={13} aria-hidden />
            {t("file.retry")}
          </button>
        )}
        <button
          type="button"
          className={iconButton}
          aria-label={t("file.download")}
          aria-describedby={nameId}
          title={t("file.download")}
          onClick={() =>
            void documentsApi.download(file.id).catch((failure) => toast.error(errorText(failure)))
          }
        >
          <Download size={15} />
        </button>
        <button
          type="button"
          className={iconButton + " hover:text-dangerink"}
          aria-label={t("file.delete")}
          aria-describedby={nameId}
          title={t("file.delete")}
          onClick={onDelete}
        >
          <Trash2 size={15} />
        </button>
      </div>
    </li>
  )
}
