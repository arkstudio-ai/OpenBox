// The voice call's endpoints (spec §5.1): a `voice` ticket, the socket it opens,
// and the two reads the entry button needs.
import { useQuery } from "@tanstack/react-query"
import { env, wsBase } from "@/shared/config/env"
import { http } from "@/shared/api/http"
import { refreshAccessToken, useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import type { AppConfig } from "@/shared/types/api"

export const VOICE_SOCKET_PATH = "/ws/assistant/voice"

export const voiceKeys = {
  /** Deliberately the chat feature's key: /api/agent/config is one request whose
   *  answer serves both, and the composer beside the button has usually read it. */
  config: (userId: string) => ["config", userId] as const,
}

function requestTicket(token: string): Promise<Response> {
  // The ticket pins its workspace, and the call talks to that workspace's
  // assistant: it has to be the one on screen, not the account's default.
  const workspaceId = useWorkspaceStore.getState().currentId
  return fetch(`${env.apiBase}/api/auth/ticket`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
      ...(workspaceId ? { "X-Workspace-Id": workspaceId } : {}),
    },
    body: JSON.stringify({ audience: "voice" }),
  })
}

/** A one-time ticket for the voice socket: the agent socket's handshake, with
 *  one retry after a token refresh. Throws when none can be had. */
export async function fetchVoiceTicket(): Promise<string> {
  const token = useAuthStore.getState().accessToken
  if (!token) throw new Error("not signed in")
  let response = await requestTicket(token)
  if (response.status === 401) {
    const refreshed = await refreshAccessToken()
    if (!refreshed) throw new Error("session expired")
    response = await requestTicket(refreshed)
  }
  if (!response.ok) throw new Error(`ticket refused: ${response.status}`)
  const { ticket } = (await response.json()) as { ticket?: unknown }
  if (typeof ticket !== "string" || !ticket) throw new TypeError("ticket missing")
  return ticket
}

export function voiceSocketUrl(ticket: string): string {
  return `${wsBase()}${VOICE_SOCKET_PATH}?${new URLSearchParams({ ticket })}`
}

/** The socket refuses a call (4404) until the assistant's main conversation exists. */
export async function ensureAssistant(): Promise<void> {
  await http.post("/api/assistant/ensure", {})
}

/** Whether this deployment takes voice calls; absent means no. */
export function useVoiceEnabled(): boolean {
  const userId = useAuthStore((state) => state.user?.id ?? "anonymous")
  const config = useQuery({
    queryKey: voiceKeys.config(userId),
    queryFn: () => http.get<AppConfig>("/api/agent/config"),
    staleTime: 5 * 60_000,
  })
  return config.data?.voice_enabled === true
}
