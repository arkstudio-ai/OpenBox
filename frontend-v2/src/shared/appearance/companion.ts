import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useAuthStore } from "@/shared/api/auth-store"
import { http, request } from "@/shared/api/http"
import { assistantPreferenceKeys } from "@/shared/api/assistant-preference-keys"
import {
  DEFAULT_COMPANION_PRESET,
  type BuiltinCompanionPreset,
  type CompanionPreset,
} from "./companion-presets"

export { COMPANION_PRESETS, type CompanionPreset } from "./companion-presets"
export interface CompanionPresetOption {
  id: BuiltinCompanionPreset
  category: "female" | "male" | "pets"
  voice_gender: "female" | "male"
}
export interface CompanionAppearance {
  enabled: boolean
  preset: CompanionPreset
  has_image: boolean
  image_url: string | null
  /** Optional while older backend instances are being upgraded. */
  presets?: CompanionPresetOption[]
}
export const DEFAULT_COMPANION: CompanionAppearance = {
  enabled: true,
  preset: DEFAULT_COMPANION_PRESET,
  has_image: false,
  image_url: null,
}
const appearanceKey = assistantPreferenceKeys.appearance
const ENDPOINT = "/api/assistant/appearance"

export function useCompanionAppearance() {
  const userId = useAuthStore((state) => state.user?.id)
  return useQuery({
    queryKey: appearanceKey(userId),
    queryFn: ({ signal }) => http.get<CompanionAppearance>(ENDPOINT, { signal }),
    enabled: !!userId,
    staleTime: 30_000,
    // Renew signed image URLs even when this browser stays open all day.
    refetchInterval: 30 * 60_000,
    retry: 1,
  })
}

export function useSaveCompanionAppearance() {
  const userId = useAuthStore((state) => state.user?.id)
  const qc = useQueryClient()
  return useMutation({
    scope: { id: `assistant-preferences:${userId}` },
    mutationFn: (patch: Partial<Pick<CompanionAppearance, "enabled" | "preset">>) => {
      if (useAuthStore.getState().user?.id !== userId) throw new Error("accountChanged")
      return http.put<CompanionAppearance>(ENDPOINT, patch)
    },
    onMutate: () => qc.cancelQueries({ queryKey: appearanceKey(userId) }),
    onSuccess: async (data) => {
      qc.setQueryData(appearanceKey(userId), data)
      await qc.invalidateQueries({ queryKey: assistantPreferenceKeys.voices(userId) })
    },
  })
}

/** Decode locally and upload only a small PNG portrait, with transparency preserved. */
async function portraitFile(file: File): Promise<File> {
  if (!/^(image\/(png|jpeg|webp|gif))$/.test(file.type) || file.size > 5 * 1024 * 1024)
    throw new Error("portraitInvalid")
  const url = URL.createObjectURL(file)
  try {
    const img = new Image()
    img.src = url
    await img.decode()
    if (!img.naturalWidth || !img.naturalHeight) throw new Error("portraitInvalid")
    const canvas = document.createElement("canvas")
    canvas.width = canvas.height = 384
    const context = canvas.getContext("2d")
    if (!context) throw new Error("portraitInvalid")
    const ratio = Math.min(384 / img.naturalWidth, 384 / img.naturalHeight)
    const width = img.naturalWidth * ratio
    const height = img.naturalHeight * ratio
    context.drawImage(img, (384 - width) / 2, (384 - height) / 2, width, height)
    const blob = await new Promise<Blob>((resolve, reject) => {
      canvas.toBlob(
        (result) => (result ? resolve(result) : reject(new Error("portraitInvalid"))),
        "image/png",
      )
    })
    return new File([blob], "portrait.png", { type: "image/png" })
  } finally {
    URL.revokeObjectURL(url)
  }
}

export function useUploadCompanionImage() {
  const userId = useAuthStore((state) => state.user?.id)
  const qc = useQueryClient()
  return useMutation({
    scope: { id: `assistant-preferences:${userId}` },
    mutationFn: async (file: File) => {
      const body = new FormData()
      body.append("file", await portraitFile(file))
      if (useAuthStore.getState().user?.id !== userId) throw new Error("accountChanged")
      return request<CompanionAppearance>(`${ENDPOINT}/image`, { method: "POST", body })
    },
    onMutate: () => qc.cancelQueries({ queryKey: appearanceKey(userId) }),
    onSuccess: async (data) => {
      qc.setQueryData(appearanceKey(userId), data)
      await qc.invalidateQueries({ queryKey: assistantPreferenceKeys.voices(userId) })
    },
  })
}
