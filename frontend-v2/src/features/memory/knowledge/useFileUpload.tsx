import { useRef, useState } from "react"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { useTranslation } from "react-i18next"
import { ApiError } from "@/shared/api/http"
import { useApiErrorMessage } from "@/shared/hooks/useApiErrorMessage"
import { toast } from "@/shared/ui/Toast"
import { useMemoryScope } from "../api"
import { documentsApi } from "../wiki/documents-api"

export const ACCEPTED_FILES = ".pdf,.docx,.txt,.md,.markdown,.csv,.html,.htm"
const MAX_BYTES = 10 * 1024 * 1024

/** One upload path for the header button and the drop zone: checks the size
 *  locally, sends files one by one to the scope in view, and reports each. */
export function useFileUpload(projectId: string, onStart?: () => void) {
  const { t } = useTranslation(["knowledge", "wiki"])
  const errorText = useApiErrorMessage()
  const { key } = useMemoryScope()
  const qc = useQueryClient()
  const input = useRef<HTMLInputElement>(null)
  const [error, setError] = useState("")
  const problem = (code: string) =>
    t(`documents.errors.${code}`, { ns: "wiki", defaultValue: t("documents.failedHint", { ns: "wiki" }) })
  const upload = useMutation({
    mutationFn: (file: File) => documentsApi.upload(file, projectId),
    onSuccess: (_doc, file) => {
      toast.success(t("file.accepted", { name: file.name }))
      void qc.invalidateQueries({ queryKey: key })
    },
  })
  const submit = async (files: File[]) => {
    if (!files.length) return
    setError("")
    onStart?.()
    for (const file of files) {
      if (!file.size || file.size > MAX_BYTES) {
        setError(problem("document_size_limit"))
        continue
      }
      try {
        await upload.mutateAsync(file)
      } catch (failure) {
        setError(
          failure instanceof ApiError && failure.code.startsWith("DOCUMENT_")
            ? problem(failure.code.toLowerCase())
            : errorText(failure),
        )
      }
    }
  }
  const picker = (
    <input
      ref={input}
      type="file"
      multiple
      className="hidden"
      aria-label={t("uploadFile")}
      accept={ACCEPTED_FILES}
      onChange={(event) => {
        const files = Array.from(event.target.files ?? [])
        event.target.value = ""
        void submit(files)
      }}
    />
  )
  return { picker, choose: () => input.current?.click(), submit, pending: upload.isPending, error }
}
