import { useAuthStore } from "@/shared/api/auth-store"
import type { Session } from "@/shared/types/api"
import { useRunningContainer } from "../api/containers"
import { useAttachments } from "./useAttachments"

/** Attachment audience is independent of the build/plan/main-assistant UI mode. */
export function useComposerAttachments(assistant: boolean, sessionId?: string, session?: Session) {
  const userId = useAuthStore((state) => state.user?.id)
  const authLoading = useAuthStore((state) => state.isLoading)
  // An existing conversation must resolve its owner and audience before any
  // upload starts. A new ordinary composer has no Session yet.
  const ready = !authLoading && !!userId && (sessionId
    ? session?.id === sessionId && session.user_id === userId
    : !assistant && !session)
  const privateAudience = assistant || session?.kind === "assistant" || session?.visibility === "private"
    || session?.assistant_managed === true || session?.memory_policy === "assistant_isolated"
  const running = useRunningContainer(ready && !privateAudience)
  const containerId = running?.id ?? null
  const canAttach = ready && (privateAudience ? !!sessionId : !!running)
  const attachments = useAttachments(containerId, privateAudience ? sessionId : undefined, {
    enabled: canAttach, allowSandboxFallback: !privateAudience,
  })
  return { attachments, containerId, canAttach }
}
