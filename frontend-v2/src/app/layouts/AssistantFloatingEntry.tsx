import { lazy, useSyncExternalStore } from "react"
import { AssistantCompanion, CompanionWorkspaceNotice, VoiceCallDock, useVoiceCall } from "@/features/voice"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"

const AssistantConversation = lazy(() => import("@/routes/workspace/AssistantRoute"))
const QuickSettings = lazy(() =>
  import("@/features/settings").then((module) => ({ default: module.AssistantQuickSettings })),
)
// A narrow desktop window (including a browser side panel) is still a PC.
// Pointer capability, rather than the sidebar breakpoint, selects this entry.
const desktop = window.matchMedia("(pointer: fine)")
const subscribe = (changed: () => void) => {
  desktop.addEventListener("change", changed)
  return () => desktop.removeEventListener("change", changed)
}
interface Props {
  surface: "assistant" | "workspace"
}

/** Composition only: reuse the existing assistant conversation, voice call, and account settings. */
export function AssistantFloatingEntry({ surface }: Props) {
  const isDesktop = useSyncExternalStore(subscribe, () => desktop.matches)
  const userId = useAuthStore((state) => state.user?.id)
  const workspaceId = useWorkspaceStore((state) => state.currentId)
  const call = useVoiceCall()
  const callWorkspace = useWorkspaceStore((state) => state.items.find((item) => item.id === call.workspaceId))
  const differentWorkspace =
    call.status !== "idle" && call.status !== "ended" && call.workspaceId !== workspaceId
  if (!isDesktop) return <VoiceCallDock />
  return (
    <AssistantCompanion key={userId} settings={QuickSettings}>
      {differentWorkspace ? (
        <CompanionWorkspaceNotice
          workspaceName={callWorkspace?.name}
          onReturn={
            callWorkspace ? () => useWorkspaceStore.getState().setCurrent(callWorkspace.id) : undefined
          }
        />
      ) : (
        <AssistantConversation key={`${userId}:${workspaceId}`} compact realtimeSurface={surface} />
      )}
    </AssistantCompanion>
  )
}
