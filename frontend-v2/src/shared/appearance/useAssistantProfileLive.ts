import { useEffect } from "react"
import { http } from "@/shared/api/http"
import { useAuthStore } from "@/shared/api/auth-store"
import { wsClient } from "@/shared/ws/client"
import { useAppearanceStore } from "./store"

/** Mount once under the workspace layout: how the person wants their assistant (and where their
 *  first meeting with it got to) follows a change at once, wherever it was made: Settings in another
 *  tab, the phone, or the assistant itself when told "以后叫你小七". A reconnect reads it again, in
 *  case the event arrived while the socket was down. */
export function useAssistantProfileLive(): void {
  const userId = useAuthStore((state) => state.user?.id)
  useEffect(() => {
    if (!userId) return
    let active = true
    const show = (view: unknown) => {
      if (active && useAuthStore.getState().user?.id === userId)
        useAppearanceStore.getState().applyAssistantView(view)
    }
    const reread = () =>
      void http
        .get<unknown>("/api/assistant/profile")
        .then(show)
        .catch(() => undefined)
    const offs = [
      wsClient.on("assistant.profile.updated", ({ userId: owner, profile }) => {
        if (owner === userId) show(profile)
      }),
      wsClient.on("__connected", reread),
    ]
    return () => { active = false; offs.forEach((off) => off()) }
  }, [userId])
}
