// WS → session-list bridge: keeps sidebar/topbar session state fresh.
import { useEffect } from "react"
import { useQueryClient } from "@tanstack/react-query"
import { wsClient } from "@/shared/ws/client"
import { useAuthStore } from "@/shared/api/auth-store"
import { workspaceKeys } from "../api/keys"
import { useWorkspaceStore } from "@/shared/api/workspace-store"

export function useWorkspaceEvents(surface: "assistant" | "workspace" = "workspace") {
  const qc = useQueryClient()
  const userId = useAuthStore((s) => s.user?.id ?? "anonymous")
  const workspaceId = useWorkspaceStore((s) => s.currentId)

  useEffect(() => {
    void wsClient.connect(surface)
    const invalidate = () => {
      void qc.invalidateQueries({ queryKey: workspaceKeys.sessions(userId, workspaceId) })
      void qc.invalidateQueries({ queryKey: ["billing", userId, workspaceId] })
    }
    const subs = [
      wsClient.on("session.status", invalidate),
      wsClient.on("session.title", invalidate),
      wsClient.on("session.updated", invalidate),
      wsClient.on("__connected", invalidate),
    ]
    return () => subs.forEach((off) => off())
  }, [qc, userId, workspaceId, surface])

  useEffect(() => {
    return () => {
      // Disconnect only when the workspace unmounts entirely (sign-out).
      wsClient.disconnect()
    }
  }, [])
}
