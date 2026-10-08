import { useRef, useState, type ReactNode } from "react"
import { useMutation, useQuery } from "@tanstack/react-query"
import { useTranslation } from "react-i18next"
import { ApiError } from "@/shared/api/http"
import { useApiErrorMessage } from "@/shared/hooks/useApiErrorMessage"
import { Dialog, DialogActions, DialogTitle } from "@/shared/ui/Dialog"
import { Spinner } from "@/shared/ui/Spinner"
import { useMemoryScope } from "../api"
import { button, field, primaryButton } from "../knowledge/ui"
import { wikiApi, type WikiEditSnapshot } from "../wiki-api"

/** Edit a page in place: its title and the plain text behind it. Saving
 *  updates the underlying memories or file in one step; the page rebuilds
 *  itself in the background. */
export function WikiEditor({
  pageId,
  onSaved,
  onClose,
}: {
  pageId: string
  onSaved: () => void
  onClose: () => void
}) {
  const { t } = useTranslation("wiki")
  const errorText = useApiErrorMessage()
  const { key } = useMemoryScope()
  const snapshot = useQuery({
    queryKey: [...key, "wiki-edit", pageId],
    queryFn: () => wikiApi.editSnapshot(pageId),
    gcTime: 0,
    staleTime: 0,
  })
  if (snapshot.data)
    return <EditorForm key={pageId} initial={snapshot.data} onSaved={onSaved} onClose={onClose} />
  return (
    <Shell onClose={onClose}>
      {snapshot.error ? (
        <>
          <p role="alert" className="bg-dangersoft text-dangerink rounded-xl px-3 py-2 text-sm">
            {errorText(snapshot.error)}
          </p>
          <DialogActions>
            <button className={button} onClick={onClose}>
              {t("consumer.cancel")}
            </button>
            <button className={primaryButton} onClick={() => void snapshot.refetch()}>
              {t("retry")}
            </button>
          </DialogActions>
        </>
      ) : (
        <p role="status" className="text-n600 flex items-center gap-2 py-6 text-sm">
          <Spinner />
          {t("loading")}
        </p>
      )}
    </Shell>
  )
}

function Shell({ onClose, children }: { onClose: () => void; children: ReactNode }) {
  const { t } = useTranslation("wiki")
  return (
    <Dialog open wide onClose={onClose} label={t("consumer.edit")}>
      <DialogTitle>{t("consumer.edit")}</DialogTitle>
      {children}
    </Dialog>
  )
}

function EditorForm({
  initial,
  onSaved,
  onClose,
}: {
  initial: WikiEditSnapshot
  onSaved: () => void
  onClose: () => void
}) {
  const { t } = useTranslation("wiki")
  const errorText = useApiErrorMessage()
  // The base revision is the one first shown; a background refresh must not
  // quietly move it forward under an unsaved edit.
  const [base] = useState(initial)
  const [title, setTitle] = useState(base.title)
  const [entries, setEntries] = useState(base.entries)
  const requestId = useRef(crypto.randomUUID())
  const save = useMutation({
    mutationFn: () => wikiApi.edit(base, title.trim(), entries, requestId.current),
    onSuccess: onSaved,
  })
  const conflict = save.error instanceof ApiError && save.error.status === 409
  const changed =
    title.trim() !== base.title || entries.some((entry, index) => entry.text !== base.entries[index]?.text)
  return (
    <Dialog open wide onClose={() => !save.isPending && onClose()} label={t("consumer.edit")}>
      <form
        className="flex flex-col gap-4"
        onSubmit={(event) => {
          event.preventDefault()
          save.mutate()
        }}
      >
        <div>
          <DialogTitle>{t("consumer.edit")}</DialogTitle>
          <p className="text-n600 mt-1.5 text-sm leading-relaxed">{t("consumer.editorHint")}</p>
        </div>
        <label className="text-n700 flex flex-col gap-1.5 text-sm">
          {t("consumer.titleLabel")}
          <input
            className={field}
            value={title}
            maxLength={160}
            required
            onChange={(event) => setTitle(event.target.value)}
            disabled={save.isPending}
          />
        </label>
        {entries.map((entry, index) => (
          <label key={entry.id} className="text-n700 flex flex-col gap-1.5 text-sm">
            {entries.length === 1 ? t("consumer.content") : t("consumer.contentNumber", { number: index + 1 })}
            <textarea
              className={field + " min-h-36 resize-y leading-relaxed"}
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
          <p role="alert" className="bg-dangersoft text-dangerink rounded-xl px-3 py-2 text-sm">
            {conflict ? t("consumer.conflict") : errorText(save.error)}
          </p>
        )}
        <DialogActions>
          <button className={button} type="button" disabled={save.isPending} onClick={onClose}>
            {t("consumer.cancel")}
          </button>
          <button
            className={primaryButton}
            type="submit"
            disabled={save.isPending || !changed || !entries.every((item) => item.text.trim()) || !title.trim()}
          >
            {t(save.isPending ? "consumer.saving" : "consumer.save")}
          </button>
        </DialogActions>
      </form>
    </Dialog>
  )
}
