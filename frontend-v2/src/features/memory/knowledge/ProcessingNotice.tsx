import { useEffect, useRef, useState } from "react"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { useTranslation } from "react-i18next"
import { Link } from "react-router"
import { AlertCircle, MessageSquare } from "lucide-react"
import { memoryApi, type MemoryProcessing } from "@/shared/api/memory"
import { useApiErrorMessage } from "@/shared/hooks/useApiErrorMessage"
import { paths } from "@/shared/router/paths"
import { Spinner } from "@/shared/ui/Spinner"
import { toast } from "@/shared/ui/Toast"
import { useMemoryScope } from "../api"
import { button, primaryButton, textButton } from "./ui"

/** What is still being saved, and what could not be — never lost silently.
 *  A failed turn shows the person's own words, with a retry or a dismissal. */
export function ProcessingNotice({ processing }: { processing: MemoryProcessing | undefined }) {
  const { t } = useTranslation("knowledge")
  const errorText = useApiErrorMessage()
  const qc = useQueryClient()
  const { key } = useMemoryScope()
  const [open, setOpen] = useState(false)
  const pending = processing?.pending ?? 0
  // A finished save adds a memory; show it without waiting for the next poll.
  const previous = useRef(pending)
  useEffect(() => {
    if (pending < previous.current) void qc.invalidateQueries({ queryKey: [...key, "list"] })
    previous.current = pending
  }, [pending, qc, key])
  const act = useMutation({
    mutationFn: async ({ ids, retry }: { ids: string[]; retry: boolean }) => {
      for (const id of ids) await (retry ? memoryApi.retryTurn(id) : memoryApi.dismissTurn(id))
    },
    onSuccess: (_, { retry }) => toast.success(t(retry ? "processing.retried" : "processing.dismissed")),
    onError: (error) => toast.error(errorText(error)),
    onSettled: () => void qc.invalidateQueries({ queryKey: [...key, "processing"] }),
  })
  const failed = processing?.failed ?? []
  if (!pending && !failed.length) return null
  return (
    <div className="mt-4 space-y-3">
      {pending > 0 && (
        <p role="status" className="text-n600 flex items-center gap-2 text-sm">
          <Spinner className="size-3.5" />
          {t("processing.pending", { count: pending })}
        </p>
      )}
      {failed.length > 0 && (
        <section className="border-hair bg-card rounded-2xl border px-4 py-3.5 sm:px-5">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div className="flex min-w-0 gap-2.5">
              <AlertCircle size={17} aria-hidden className="text-dangerink mt-0.5 flex-none" />
              <div className="min-w-0">
                <h2 className="text-ink text-sm font-medium">
                  {t("processing.failedTitle", { count: failed.length })}
                </h2>
                <p className="text-n600 mt-0.5 text-xs leading-relaxed">{t("processing.failedHint")}</p>
              </div>
            </div>
            <div className="flex flex-none gap-2">
              <button type="button" className={button} aria-expanded={open} onClick={() => setOpen(!open)}>
                {t(open ? "processing.hide" : "processing.show")}
              </button>
              <button
                type="button"
                className={primaryButton}
                disabled={act.isPending}
                onClick={() => act.mutate({ ids: failed.map((item) => item.id), retry: true })}
              >
                {t("processing.retryAll")}
              </button>
            </div>
          </div>
          {open && (
            <ul className="divide-hair mt-3 divide-y">
              {failed.map((item) => (
                <li key={item.id} className="py-3 first:pt-1 last:pb-0">
                  <p className="text-ink text-sm leading-relaxed break-words">“{item.excerpt}”</p>
                  <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
                    <Link
                      to={paths.chat(item.session_id)}
                      className="text-a700 inline-flex items-center gap-1 hover:underline"
                    >
                      <MessageSquare size={12} aria-hidden />
                      {item.session_title || t("processing.untitled")}
                    </Link>
                    <button
                      type="button"
                      className={textButton}
                      disabled={act.isPending}
                      onClick={() => act.mutate({ ids: [item.id], retry: true })}
                    >
                      {t("processing.retry")}
                    </button>
                    <button
                      type="button"
                      className={textButton}
                      disabled={act.isPending}
                      onClick={() => act.mutate({ ids: [item.id], retry: false })}
                    >
                      {t("processing.dismiss")}
                    </button>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </section>
      )}
    </div>
  )
}
