import { useRef, useState } from "react"
import { useInfiniteQuery, useMutation, useQueryClient } from "@tanstack/react-query"
import { useTranslation } from "react-i18next"
import { Link } from "react-router"
import { FileText, Upload } from "lucide-react"
import { ApiError } from "@/shared/api/http"
import { paths } from "@/shared/router/paths"
import { useApiErrorMessage } from "@/shared/hooks/useApiErrorMessage"
import { memoryButton } from "@/shared/ui/MemoryDiagnostics"
import { useMemoryScope } from "../api"
import { documentsApi } from "./documents-api"

export function WikiUpload({ projectId, enabled }: { projectId: string; enabled: boolean }) {
  const { t } = useTranslation("wiki")
  const errorText = useApiErrorMessage()
  const { key } = useMemoryScope()
  const qc = useQueryClient()
  const input = useRef<HTMLInputElement>(null)
  const [localError, setLocalError] = useState("")
  const files = useInfiniteQuery({
    queryKey: [...key, "documents", projectId],
    queryFn: ({ pageParam }) => documentsApi.list(projectId, pageParam),
    initialPageParam: 0,
    getNextPageParam: (last) => last.next_offset ?? undefined,
    enabled,
    refetchInterval: 5000,
  })
  const upload = useMutation({
    mutationFn: (file: File) => documentsApi.upload(file, projectId),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: key })
    },
  })
  const retry = useMutation({
    mutationFn: documentsApi.retry,
    onSuccess: () => {
      void files.refetch()
    },
  })
  const problem = (code: string) => t(`documents.errors.${code}`, { defaultValue: t("documents.failedHint") })
  const error = upload.error ?? retry.error
  const rows = files.data?.pages.flatMap((page) => page.documents) ?? []
  return (
    <section
      className="border-hair bg-card space-y-3 rounded-2xl border p-4"
      aria-label={t("documents.title")}
    >
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-sm font-medium">{t("documents.title")}</h2>
          <p className="text-n500 mt-1 text-xs leading-5">{t("documents.hint")}</p>
        </div>
        <input
          ref={input}
          type="file"
          className="hidden"
          aria-label={t("documents.upload")}
          accept=".pdf,.docx,.txt,.md,.markdown,.csv,.html,.htm"
          onChange={(event) => {
            const file = event.target.files?.[0]
            event.target.value = ""
            if (!file) return
            upload.reset()
            setLocalError("")
            if (file.size > 10 * 1024 * 1024 || !file.size) {
              setLocalError(problem("document_size_limit"))
              return
            }
            upload.mutate(file)
          }}
        />
        <button
          className={memoryButton + " inline-flex items-center gap-2"}
          disabled={!enabled || upload.isPending}
          onClick={() => input.current?.click()}
        >
          <Upload className="size-4" />
          {t(upload.isPending ? "documents.uploading" : "documents.upload")}
        </button>
      </div>
      {(localError || error) && (
        <p role="alert" className="text-danger text-sm">
          {localError ||
            (error instanceof ApiError && error.code.startsWith("DOCUMENT_")
              ? problem(error.code.toLowerCase())
              : errorText(error))}
        </p>
      )}
      {upload.isSuccess && (
        <p role="status" className="text-a700 text-sm">
          {t("documents.accepted")}
        </p>
      )}
      {files.error && (
        <p role="alert" className="text-danger text-sm">
          {errorText(files.error)}
        </p>
      )}
      {rows.length > 0 && (
        <details open className="border-hair border-t pt-3">
          <summary className="text-n600 cursor-pointer text-xs">{t("documents.files")}</summary>
          <ul className="mt-2 space-y-2">
            {rows.map((file) => (
              <li key={file.id} className="bg-rail/40 flex flex-wrap items-center gap-3 rounded-lg px-3 py-2">
                <FileText className="text-n500 size-4 shrink-0" />
                <div className="min-w-0 flex-1">
                  <p className="truncate text-sm">{file.filename}</p>
                  <p className="text-n500 text-xs leading-5" role="status">
                    {t(`documents.status.${file.status}`, { defaultValue: t("documents.status.pending") })}
                  </p>
                  {file.reason_code && <p className="text-danger text-xs">{problem(file.reason_code)}</p>}
                </div>
                {file.page_ids.length > 0 && (
                  <Link className={memoryButton} to={paths.wikiPage(file.page_ids[0], projectId)}>
                    {t("readPage")}
                  </Link>
                )}
                {["failed", "index_failed"].includes(file.status) && (
                  <button
                    className={memoryButton}
                    disabled={retry.isPending}
                    onClick={() => retry.mutate(file.id)}
                  >
                    {t("retry")}
                  </button>
                )}
              </li>
            ))}
          </ul>
          {files.hasNextPage && (
            <button
              className={memoryButton + " mt-3"}
              disabled={files.isFetchingNextPage}
              onClick={() => void files.fetchNextPage()}
            >
              {t("loadMore")}
            </button>
          )}
        </details>
      )}
    </section>
  )
}
