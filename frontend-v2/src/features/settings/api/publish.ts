// 视频发布 route setting. Videos reach Douyin one of two ways: "desktop" —
// the agent fills the 创作者中心 form on the cloud desktop (fully automatic,
// internally publish mode `auto`); "api" — the 开放平台 QR posting package the
// person scans and confirms in the Douyin app (publish mode `package`). No
// choice means the deployment default applies. The backend reads the same
// preference in desktop_publish's precheck, so what is shown here is what
// the agent does. Components never fetch directly (ENGINEERING_SPEC §7).
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { http } from "@/shared/api/http"
import { useAuthStore } from "@/shared/api/auth-store"
import { settingsKeys } from "./keys"

export type PublishRoute = "desktop" | "api"

export interface PublishRouteStatus {
  /** The stored choice; null = follow the deployment default. */
  preference: PublishRoute | null
  deploymentDefault: PublishRoute
  /** What applies right now: the choice, else the deployment default. */
  effective: PublishRoute
  routes: PublishRoute[]
}

function useUserId(): string {
  return useAuthStore((s) => s.user?.id ?? "anonymous")
}

export function usePublishRoute() {
  const userId = useUserId()
  return useQuery({
    queryKey: settingsKeys.publish(userId),
    queryFn: () => http.get<PublishRouteStatus>("/api/publish/preference"),
    staleTime: 30_000,
  })
}

/** Store a route, or `null` to go back to the deployment default. */
export function useUpdatePublishRoute() {
  const userId = useUserId()
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (route: PublishRoute | null) =>
      http.put<PublishRouteStatus>("/api/publish/preference", { route }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: settingsKeys.publish(userId) }),
  })
}
