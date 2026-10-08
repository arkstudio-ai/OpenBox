import { useEffect } from "react"
import { http } from "@/shared/api/http"
import { wsClient } from "@/shared/ws/client"
import { useAppearanceStore } from "./store"

/** Mount once under the workspace layout: the assistant's name follows a rename at once, wherever it
 *  happened (Settings in another tab, the phone, or the assistant renaming itself in chat). A
 *  reconnect reads it again, in case the event arrived while the socket was down. */
export function useAssistantNameLive(): void {
  useEffect(() => {
    const show = (name: string) => useAppearanceStore.setState({ assistantName: name })
    const reread = () =>
      void http
        .get<{ name: string }>("/api/assistant/name")
        .then(({ name }) => show(name))
        .catch(() => undefined)
    const offs = [wsClient.on("assistant.renamed", ({ name }) => show(name)), wsClient.on("__connected", reread)]
    return () => offs.forEach((off) => off())
  }, [])
}
