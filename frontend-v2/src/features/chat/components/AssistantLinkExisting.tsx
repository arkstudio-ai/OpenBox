import { useState } from "react"
import { useQueryClient } from "@tanstack/react-query"
import { useTranslation } from "react-i18next"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import { useApiErrorMessage } from "@/shared/hooks/useApiErrorMessage"
import { ApiError } from "@/shared/api/http"
import { assistantKeys, useAssistantLinkCandidates, type AssistantLinkCandidate } from "../api/assistant"
import { linkExisting } from "../lib/link-existing"

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
    if (sending) return
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
      {candidates.isPending ? <p role="status">{t("assistant.loadingTask")}</p> : items.map((item) =>
        <div key={item.id} className="border-hair flex items-center justify-between gap-3 rounded-lg border p-3">
          <div className="min-w-0 text-sm"><p className="break-words">{item.title || item.id}</p>
            <p className="text-n600 break-words text-xs">{item.project_name}</p>
            {!item.link.available && <p className="text-n600 text-xs">{t(`assistant.link.reasons.${item.link.reason_code}`, { defaultValue: t("assistant.link.unavailable") })}</p>}
          </div>
          <button type="button" className="shrink-0 text-sm underline disabled:opacity-50"
            disabled={!!sending || !item.link.available || !!item.link.task_id && !item.link.archived}
            onClick={() => void submit(item)}>{sending === item.id ? t("assistant.link.linking")
              : item.link.task_id && !item.link.archived ? t("assistant.link.linked")
                : item.link.archived ? t("assistant.link.reopen") : t("assistant.link.action")}</button>
        </div>)}
      {!candidates.isPending && items.length === 0 && <p>{t("assistant.link.empty")}</p>}
      <button type="button" className="text-sm underline" disabled={!!sending || candidates.isFetching}
        onClick={() => void candidates.refetch()}>{t("assistant.reload")}</button>
      {candidates.hasNextPage && <button type="button" className="ml-4 text-sm underline" disabled={candidates.isFetchingNextPage}
        onClick={() => void candidates.fetchNextPage()}>{t("assistant.moreTasks")}</button>}
    </div>}
  </div>
}
