import { useRef, useState } from "react"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { useTranslation } from "react-i18next"
import { ApiError } from "@/shared/api/http"
import { memoryApi, type MemoryRecord } from "@/shared/api/memory"
import { useApiErrorMessage } from "@/shared/hooks/useApiErrorMessage"
import { toast } from "@/shared/ui/Toast"
import { useMemoryScope } from "../api"

export type MemoryDialog =
  { kind: "create" } | { kind: "edit"; memory: MemoryRecord } | { kind: "forget"; memory: MemoryRecord }

type Write =
  | { kind: "create"; summary: string; projectId: string }
  | { kind: "edit"; memory: MemoryRecord; summary: string }
  | { kind: "forget"; memory: MemoryRecord; sourceIds?: string[] }

/** What a forgotten memory looks like locally: no text, no revision to reuse. */
const forgotten = (memory: MemoryRecord): MemoryRecord => ({
  ...memory,
  summary: "",
  value: undefined,
  status: "DEPRECATED",
  body_available: false,
})

/** Add, edit and forget, plus which dialog and detail sheet are open.
 *
 *  One request id is kept until the server answers, so a retried click is the
 *  same command rather than a second one. Edits name the revision on screen;
 *  a conflict keeps the dialog — and the person's text — open. */
export function useMemoryActions() {
  const { t } = useTranslation("knowledge")
  const errorText = useApiErrorMessage()
  const { key } = useMemoryScope()
  const qc = useQueryClient()
  const [dialog, setDialog] = useState<MemoryDialog | null>(null)
  const [detail, setDetail] = useState<MemoryRecord | null>(null)
  const requestId = useRef<string | null>(null)
  const write = useMutation({
    mutationFn: async (input: Write) => {
      requestId.current ??= crypto.randomUUID()
      const id = requestId.current
      if (input.kind === "create") return memoryApi.create(input.summary, input.projectId, id)
      if (input.kind === "edit") return memoryApi.correct(input.memory, input.summary, id)
      return memoryApi.forget(input.memory, id, input.sourceIds)
    },
    onSuccess: (result, input) => {
      requestId.current = null
      setDialog(null)
      if (input.kind === "forget") {
        toast.success(t("forget.done"))
        setDetail((current) => (current?.id === input.memory.id ? forgotten(input.memory) : current))
        // Drop this memory's warm snapshots outright; invalidation alone would
        // keep serving their old bodies until the refetch lands.
        for (const part of ["sources", "history", "cleanup"])
          void qc.resetQueries({ queryKey: [...key, part, input.memory.id] })
      } else {
        toast.success(t(input.kind === "create" ? "editor.created" : "editor.saved"))
        if (input.kind === "edit")
          setDetail((current) =>
            current?.id === input.memory.id && "revision" in result ? (result as MemoryRecord) : current,
          )
      }
      void qc.invalidateQueries({ queryKey: key })
    },
    onError: (error) => {
      if (error instanceof ApiError && error.status < 500) requestId.current = null
      if (error instanceof ApiError && error.status === 409) void qc.invalidateQueries({ queryKey: key })
    },
  })
  const open = (next: MemoryDialog) => {
    requestId.current = null
    write.reset()
    setDialog(next)
  }
  const error =
    write.error instanceof ApiError && write.error.status === 409
      ? t("editor.conflict")
      : write.error instanceof ApiError && write.error.code === "MEMORY_SENSITIVE_CONTENT"
        ? t("editor.sensitive")
        : write.error
          ? errorText(write.error)
          : null
  return { dialog, open, close: () => setDialog(null), detail, setDetail, write, error }
}
