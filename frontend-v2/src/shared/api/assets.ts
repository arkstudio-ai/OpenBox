// Trading an asset id for a fresh preview URL is not a chat concern: any
// feature that surfaces a produced file needs it, and presigned GETs expire so
// nobody can hold a URL. Lives in shared rather than in one feature because
// features must not import from each other (§4.1).
import { useQuery } from "@tanstack/react-query"
import { http } from "@/shared/api/http"
import { useAuthStore } from "@/shared/api/auth-store"

export interface AssetUrl {
  url: string
  mime: string
  name: string
  size: number
}

export function useAssetUrl(assetId: string | null | undefined) {
  const userId = useAuthStore((s) => s.user?.id ?? "anonymous")
  return useQuery({
    queryKey: ["asset-url", userId, assetId ?? "none"] as const,
    queryFn: () => http.get<AssetUrl>(`/api/assets/${assetId}/url`),
    enabled: !!assetId,
    // Presigned GETs live an hour; refresh well inside that.
    staleTime: 40 * 60_000,
    retry: 1,
  })
}
