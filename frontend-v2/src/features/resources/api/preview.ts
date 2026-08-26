// Text body for the preview pane. The bucket does not allow a cross-origin
// read, so this one payload comes back through the API (capped server-side).
import { useQuery } from "@tanstack/react-query"
import { http } from "@/shared/api/http"
import { useAuthStore } from "@/shared/api/auth-store"

interface ResourceText {
  name: string
  mime: string
  text: string
  truncated: boolean
}

export function useResourceText(id: string | null, enabled: boolean) {
  const userId = useAuthStore((s) => s.user?.id ?? "anonymous")
  return useQuery({
    queryKey: ["resource-text", userId, id ?? "none"] as const,
    queryFn: () => http.get<ResourceText>(`/api/assets/${id}/text`),
    enabled: enabled && !!id,
    staleTime: 5 * 60_000,
    retry: false,
  })
}
