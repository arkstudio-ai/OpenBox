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
  model: string
  default_model: string
  models: { id: string; name: string; tier: "expert" | "standard" }[]
  voices: AssistantVoice[]
  default: string
  selected: string
  voice_source?: "user" | "appearance" | "model_default"
}

function useUserId(): string {
  return useAuthStore((s) => s.user?.id ?? "anonymous")
}

export function useAssistantVoices() {
  const userId = useUserId()
  return useQuery({
    queryKey: settingsKeys.voices(userId),
    queryFn: ({ signal }) => http.get<AssistantVoices>("/api/assistant/voice/voices", { signal }),
    staleTime: 60_000,
  })
}

export function useChooseVoice() {
  const userId = useUserId()
  const qc = useQueryClient()
  return useMutation({
    scope: { id: `assistant-preferences:${userId}` },
    mutationFn: (choice: { model?: string; voice?: string }) => {
      if (useAuthStore.getState().user?.id !== userId) throw new Error("accountChanged")
      return http.put<AssistantVoices>("/api/assistant/voice/voice", choice)
    },
    onMutate: () => qc.cancelQueries({ queryKey: settingsKeys.voices(userId) }),
    onSuccess: async (data) => {
      await qc.cancelQueries({ queryKey: settingsKeys.voices(userId) })
      qc.setQueryData(settingsKeys.voices(userId), data)
    },
  })
}

/** A short recording of the voice (public, like any static asset). */
export function voiceSampleUrl(id: string, model?: string): string {
  return `/api/assistant/voice/samples/${encodeURIComponent(id)}${model ? `?model=${encodeURIComponent(model)}` : ""}`
}
