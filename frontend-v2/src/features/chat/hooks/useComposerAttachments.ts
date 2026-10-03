import { useRunningContainer } from "../api/containers"
import { useAttachments } from "./useAttachments"

/** Main-assistant attachments stay in the asset service; opening the composer never looks up a desktop. */
export function useComposerAttachments(assistant: boolean, sessionId?: string) {
  const running = useRunningContainer(!assistant)
  const containerId = running?.id ?? null
  const attachments = useAttachments(containerId, assistant ? sessionId : undefined)
  return { attachments, containerId, canAttach: assistant ? !!sessionId : !!running }
}
