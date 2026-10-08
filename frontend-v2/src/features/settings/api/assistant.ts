// 个人助理 setting: what the assistant has learned about how the person likes to be helped
// (backend assistant/style.py), each item removable like any memory.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { http } from "@/shared/api/http"
import { useAuthStore } from "@/shared/api/auth-store"
import { memoryApi } from "@/shared/api/memory"
import type { ReactionReason } from "@/shared/types/api"
import { settingsKeys } from "./keys"

export interface LearnedItem {
  id: string
  revision: number
  summary: string
  updated_at: string | null
}

export interface LearnedStyle {
  learned: LearnedItem[]
  /** Reasons picked with thumbs-downs in the assistant's chat over the last 30 days. */
  reactions: { reason: ReactionReason; count: number }[]
}

function useUserId(): string {
  return useAuthStore((s) => s.user?.id ?? "anonymous")
}

export function useLearnedStyle() {
  const userId = useUserId()
  return useQuery({
    queryKey: settingsKeys.assistantLearned(userId),
    queryFn: () => http.get<LearnedStyle>("/api/assistant/profile/learned"),
    staleTime: 30_000,
  })
}

/** Forget one learned item, as the 知识库 does: it stops being followed and is not learned again. */
export function useForgetLearned() {
  const userId = useUserId()
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (item: LearnedItem) => memoryApi.forget(item, crypto.randomUUID()),
    onSuccess: () => void qc.invalidateQueries({ queryKey: settingsKeys.assistantLearned(userId) }),
  })
}
