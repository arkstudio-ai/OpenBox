import { useState } from "react"
import { useQueryClient } from "@tanstack/react-query"
import { useTranslation } from "react-i18next"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import { useApiErrorMessage } from "@/shared/hooks/useApiErrorMessage"
import { ApiError } from "@/shared/api/http"
import { assistantKeys, useAssistantLinkCandidates, type AssistantLinkCandidate } from "../api/assistant"
import { linkExisting } from "../lib/link-existing"

const LABEL = "bg-n200/60 text-n700 rounded px-1.5 py-0.5 text-2xs leading-3.5 whitespace-nowrap"

export function AssistantLinkExisting() {
  const user = useAuthStore((state) => state.user?.id)
  const workspace = useWorkspaceStore((state) => state.currentId)
  return user && workspace ? <Picker key={`${user}:${workspace}`} user={user} workspace={workspace} /> : null
}

function Picker({ user, workspace }: { user: string; workspace: string }) {
  const { t } = useTranslation("chat")
  const [open, setOpen] = useState(false)
  const [sending, setSending] = useState<string | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [linked, setLinked] = useState<string | null>(null)
  const candidates = useAssistantLinkCandidates(open)
  const qc = useQueryClient()
  const errorMessage = useApiErrorMessage()
  const items = Array.from(new Map(candidates.data?.pages.flatMap((page) => page.items.map((item) => [item.id, item] as const))).values())
  const current = () => useAuthStore.getState().user?.id === user && useWorkspaceStore.getState().currentId === workspace
  const submit = async (item: AssistantLinkCandidate) => {
    if (sending || !item.link) return
    setSending(item.id); setError(null); setLinked(null)
    try {
      const receipt = await linkExisting(user, workspace, item.id, item.link.version)
      if (receipt && current()) {
        setLinked(item.title || item.id)
        await qc.invalidateQueries({ queryKey: assistantKeys.all(user, workspace) })
      }
    } catch (failure) {
      if (current()) {
        setError(failure)
        if (failure instanceof ApiError && failure.status === 409) void candidates.refetch()
      }
    } finally { if (current()) setSending(null) }
  }
  return <div className="py-2">
    <button type="button" className="text-n600 text-sm underline" aria-expanded={open}
      onClick={() => setOpen(!open)}>{t("assistant.link.title")}</button>
    {open && <div className="space-y-3 py-3" aria-label={t("assistant.link.title")}>
      <p className="text-n600 text-xs">{t("assistant.link.description")}</p>
      {(error || candidates.error) && <p role="alert" className="text-sm">{errorMessage(error || candidates.error)}</p>}
      {linked && <p role="status" className="text-sm">{t("assistant.link.success", { title: linked })}</p>}
      {/* The server pages newest first; keep its order. */}
      {candidates.isPending ? <p role="status">{t("assistant.loadingTask")}</p> : items.map((item) => {
        const watched = item.watched ?? (!!item.link?.task_id && !item.link.archived)
        const available = !!item.link?.available
        return <div key={item.id} className="border-hair flex items-center justify-between gap-3 rounded-lg border p-3">
          <div className="min-w-0 text-sm"><p className="break-words">{item.title || item.id}</p>
            <p className="text-n600 flex flex-wrap items-center gap-1.5 break-words text-xs">
              <span>{item.project_name}</span>
              {item.visibility === "workspace" && <span className={LABEL}>{t("assistant.link.workspaceVisible")}</span>}
              {watched && <span className={LABEL}>{t("assistant.link.watched")}</span>}
            </p>
            {!watched && !available && <p className="text-n600 text-xs">{item.link?.reason_code
              ? t(`assistant.link.reasons.${item.link.reason_code}`, { defaultValue: t("assistant.link.unavailable") })
              : t("assistant.link.unavailable")}</p>}
          </div>
          {/* Already watched: nothing to do here; stop watching from its task card. */}
          {!watched && <button type="button" className="shrink-0 text-sm underline disabled:opacity-50"
            disabled={!!sending || !available} onClick={() => void submit(item)}>
            {sending === item.id ? t("assistant.link.linking") : item.link?.archived ? t("assistant.link.reopen") : t("assistant.link.action")}
          </button>}
        </div>
      })}
      {!candidates.isPending && items.length === 0 && <p>{t("assistant.link.empty")}</p>}
      <button type="button" className="text-sm underline" disabled={!!sending || candidates.isFetching}
        onClick={() => void candidates.refetch()}>{t("assistant.reload")}</button>
      {candidates.hasNextPage && <button type="button" className="ml-4 text-sm underline" disabled={candidates.isFetchingNextPage}
        onClick={() => void candidates.fetchNextPage()}>{t("assistant.moreTasks")}</button>}
    </div>}
  </div>
}
