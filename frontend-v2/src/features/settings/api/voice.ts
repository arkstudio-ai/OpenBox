// 语音通话 setting: the voice the personal assistant speaks with in a call.
// The list is the server's (voices checked on the call model); a pick is
// stored in the person's preferences and the next call uses it.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { http } from "@/shared/api/http"
import { useAuthStore } from "@/shared/api/auth-store"
import { settingsKeys } from "./keys"

export interface AssistantVoice {
  /** The provider's voice id, exactly (e.g. "Liora Mira"). */
  id: string
  /** Its Chinese name in Alibaba Cloud's list (e.g. 清欢). */
  name: string
  gender: "female" | "male"
  /** The language it is first meant for; every voice speaks both. */
  lang: "zh" | "en"
  description: string
  description_en: string
}

export interface AssistantVoices {
  voices: AssistantVoice[]
  default: string
  selected: string
}

function useUserId(): string {
  return useAuthStore((s) => s.user?.id ?? "anonymous")
}

export function useAssistantVoices() {
  const userId = useUserId()
  return useQuery({
    queryKey: settingsKeys.voices(userId),
    queryFn: () => http.get<AssistantVoices>("/api/assistant/voice/voices"),
    staleTime: 60_000,
  })
}

export function useChooseVoice() {
  const userId = useUserId()
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (voice: string) => http.put<{ selected: string }>("/api/assistant/voice/voice", { voice }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: settingsKeys.voices(userId) }),
  })
}

/** A short recording of the voice (public, like any static asset). */
export function voiceSampleUrl(id: string): string {
  return `/api/assistant/voice/samples/${encodeURIComponent(id)}`
}
