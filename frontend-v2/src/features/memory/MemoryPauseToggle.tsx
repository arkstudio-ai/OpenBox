import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useTranslation } from "react-i18next"
import { Brain } from "lucide-react"
import { memoryApi } from "@/shared/api/memory"
import { useApiErrorMessage } from "@/shared/hooks/useApiErrorMessage"
import { cn } from "@/shared/lib/cn"
import { toast } from "@/shared/ui/Toast"
import { useMemoryScope } from "./api"

/** Keep one chat out of memory: nothing said in it is remembered while paused.
 *  Shows the account-wide state instead when automatic saving is off. */
export function MemoryPauseToggle({ sessionId }: { sessionId: string | null | undefined }) {
  const { t } = useTranslation("workspace")
  const errorText = useApiErrorMessage()
  const qc = useQueryClient()
  const { key } = useMemoryScope()
  const queryKey = [...key, "memory-settings", sessionId]
  const settings = useQuery({
    queryKey,
    queryFn: () => memoryApi.settings(sessionId ?? undefined),
    enabled: !!sessionId,
    retry: false,
  })
  const toggle = useMutation({
    mutationFn: (paused: boolean) => memoryApi.pauseChat(sessionId!, paused),
    onSuccess: (next) => {
      qc.setQueryData(queryKey, next)
      toast.success(t(next.session_paused ? "memoryPause.paused" : "memoryPause.resumed"))
    },
    onError: (error) => toast.error(errorText(error)),
  })
  if (!sessionId || !settings.data) return null
  if (!settings.data.auto_save)
    return (
      <span className="text-n600 bg-hairsoft hidden flex-none items-center gap-1 rounded-full px-2.5 py-1 text-xs sm:inline-flex">
        <Brain size={13} aria-hidden />
        {t("memoryPause.accountOff")}
      </span>
    )
  const paused = settings.data.session_paused
  return (
    <button
      type="button"
      aria-pressed={paused}
      title={t(paused ? "memoryPause.resumeHint" : "memoryPause.pauseHint")}
      disabled={toggle.isPending}
      onClick={() => toggle.mutate(!paused)}
      className={cn(
        "flex h-8 flex-none items-center justify-center gap-1 rounded-full text-xs transition-colors",
        paused ? "bg-hairsoft text-n700 px-2.5" : "text-n700 hover:bg-hairsoft w-8",
      )}
    >
      <Brain size={15} strokeWidth={2.2} aria-hidden className={paused ? "opacity-50" : undefined} />
      {paused ? t("memoryPause.pausedLabel") : <span className="sr-only">{t("memoryPause.pauseHint")}</span>}
    </button>
  )
}
