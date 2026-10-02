import { useRef, useState } from "react"
import { useMutation, useQuery } from "@tanstack/react-query"
import { useTranslation } from "react-i18next"
import { ApiError } from "@/shared/api/http"
import { memoryApi } from "@/shared/api/memory"
import { useApiErrorMessage } from "@/shared/hooks/useApiErrorMessage"
import { memoryButton, memoryInput, memoryPrimary } from "@/shared/ui/MemoryDiagnostics"
import { useMemoryScope } from "../api"
import { wikiApi, type WikiEditSnapshot } from "../wiki-api"

export function WikiEditor({
  pageId,
  projectId,
  onSaved,
  onClose,
}: {
  pageId?: string
  projectId: string
  onSaved: () => void
  onClose: () => void
}) {
  const { t } = useTranslation("wiki")
  const errorText = useApiErrorMessage()
  const { key } = useMemoryScope()
  const snapshot = useQuery({
    queryKey: [...key, "wiki-edit", pageId],
    queryFn: () => wikiApi.editSnapshot(pageId!),
    enabled: !!pageId,
    gcTime: 0,
    staleTime: 0,
  })
  if (pageId && snapshot.isPending) return <p role="status">{t("loading")}</p>
  if (pageId && snapshot.error)
    return (
      <div className="space-y-4">
        <p role="alert" className="text-danger">
          {errorText(snapshot.error)}
        </p>
        <button className={memoryButton} onClick={() => void snapshot.refetch()}>
          {t("retry")}
        </button>
        <button className={memoryButton} onClick={onClose}>
          {t("consumer.cancel")}
        </button>
      </div>
    )
  return (
    <EditorForm
      key={pageId ?? "new"}
      initial={snapshot.data}
      projectId={projectId}
      onSaved={onSaved}
      onClose={onClose}
    />
  )
}

function EditorForm({
  initial,
  projectId,
  onSaved,
  onClose,
}: {
  initial?: WikiEditSnapshot
  projectId: string
  onSaved: () => void
  onClose: () => void
}) {
  const { t } = useTranslation("wiki")
  const errorText = useApiErrorMessage()
  const [base] = useState(initial)
  const [title, setTitle] = useState(base?.title ?? "")
  const [entries, setEntries] = useState(
    base?.entries ?? [{ id: "new", revision: 0, text: "", max_length: 2000 }],
  )
  const requestId = useRef(crypto.randomUUID())
  const save = useMutation({
    mutationFn: () =>
      base
        ? wikiApi.edit(base, title.trim(), entries, requestId.current)
        : memoryApi.create(entries[0].text.trim(), projectId, requestId.current),
    onSuccess: onSaved,
  })
  const conflict = save.error instanceof ApiError && save.error.status === 409
  return (
    <form
      className="border-hair bg-card space-y-5 rounded-2xl border p-5 sm:p-7"
      aria-label={t(base ? "consumer.edit" : "consumer.add")}
      onSubmit={(event) => {
        event.preventDefault()
        save.mutate()
      }}
    >
      <div>
        <h2 className="text-xl font-semibold">{t(base ? "consumer.edit" : "consumer.add")}</h2>
        <p className="text-n600 mt-2 text-sm leading-6">{t("consumer.editorHint")}</p>
      </div>
      {base && (
        <label className="flex flex-col gap-2 text-sm">
          {t("consumer.titleLabel")}
          <input
            className={memoryInput}
            value={title}
            maxLength={160}
            required
            onChange={(event) => setTitle(event.target.value)}
            disabled={save.isPending}
          />
        </label>
      )}
      {entries.map((entry, index) => (
        <label key={entry.id} className="flex flex-col gap-2 text-sm">
          {entries.length === 1 ? t("consumer.content") : t("consumer.contentNumber", { number: index + 1 })}
          <textarea
            className={memoryInput + " min-h-40 w-full resize-y leading-7"}
            value={entry.text}
            maxLength={entry.max_length}
            required
            disabled={save.isPending}
            onChange={(event) =>
              setEntries((current) =>
                current.map((item) => (item.id === entry.id ? { ...item, text: event.target.value } : item)),
              )
            }
          />
        </label>
      ))}
      {save.error && (
        <p role="alert" className="text-danger text-sm">
          {conflict ? t("consumer.conflict") : errorText(save.error)}
        </p>
      )}
      <div className="flex gap-3">
        <button
          className={memoryPrimary}
          type="submit"
          disabled={save.isPending || !entries.every((item) => item.text.trim()) || (!!base && !title.trim())}
        >
          {t(save.isPending ? "consumer.saving" : "consumer.save")}
        </button>
        <button className={memoryButton} type="button" disabled={save.isPending} onClick={onClose}>
          {t("consumer.cancel")}
        </button>
      </div>
    </form>
  )
}
