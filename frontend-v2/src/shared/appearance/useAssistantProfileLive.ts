import { useEffect } from "react"
import { http } from "@/shared/api/http"
import { wsClient } from "@/shared/ws/client"
import { readAssistantProfile, type AssistantProfile } from "./assistant-profile"
import { useAppearanceStore } from "./store"

/** Mount once under the workspace layout: how the person wants their assistant follows a change at
 *  once, wherever it was made (Settings in another tab, the phone, or the assistant itself when told
 *  "以后叫你小七"). A reconnect reads it again, in case the event arrived while the socket was down. */
export function useAssistantProfileLive(): void {
  useEffect(() => {
    const show = (profile: unknown) =>
      useAppearanceStore.setState({ assistant: readAssistantProfile(profile) })
    const reread = () =>
      void http
        .get<AssistantProfile>("/api/assistant/profile")
        .then(show)
        .catch(() => undefined)
    const offs = [
      wsClient.on("assistant.profile.updated", ({ profile }) => show(profile)),
      wsClient.on("__connected", reread),
    ]
    return () => offs.forEach((off) => off())
  }, [])
}
