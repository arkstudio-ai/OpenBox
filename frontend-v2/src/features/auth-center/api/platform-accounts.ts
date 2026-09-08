// 授权中心 data hooks. Components never fetch directly (ENGINEERING_SPEC §7).
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { http } from "@/shared/api/http"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import type {
  NotificationPage,
  Platform,
  PlatformAccount,
  PublishJob,
  PublishResult,
  VideoAsset,
} from "@/features/auth-center/types"
import { authCenterKeys } from "./keys"

function useUserId(): string {
  return useAuthStore((s) => s.user?.id ?? "anon")
}

function useWorkspaceId(): string {
  return useWorkspaceStore((s) => s.currentId ?? "none")
}

/** Whether the person may bind or unbind accounts in the selected workspace. */
export function useCanManageAccounts(): boolean {
  const items = useWorkspaceStore((s) => s.items)
  const currentId = useWorkspaceStore((s) => s.currentId)
  const role = items.find((w) => w.id === currentId)?.role
  // No workspace list yet (single-user deployments) — let the backend decide.
  if (!role) return true
  return role === "owner" || role === "admin"
}

export function usePlatforms() {
  const userId = useUserId()
  return useQuery({
    queryKey: authCenterKeys.platforms(userId),
    queryFn: () => http.get<Platform[]>("/api/platforms"),
    staleTime: 5 * 60_000,
  })
}

export function usePlatformAccounts() {
  const userId = useUserId()
  const workspaceId = useWorkspaceId()
  return useQuery({
    queryKey: authCenterKeys.accounts(userId, workspaceId),
    queryFn: () => http.get<PlatformAccount[]>("/api/platform-accounts"),
    // A bind completes in another tab (the OAuth redirect), so always re-ask.
    refetchOnMount: "always",
    refetchOnWindowFocus: true,
  })
}

function useRefreshAccounts() {
  const qc = useQueryClient()
  const userId = useUserId()
  const workspaceId = useWorkspaceId()
  return () => {
    void qc.invalidateQueries({ queryKey: authCenterKeys.accounts(userId, workspaceId) })
  }
}

/** Ask the backend for the platform's authorize URL, then leave the app for it. */
export function useStartAuthorize() {
  return useMutation({
    mutationFn: (platform: string) =>
      http.post<{ authorizeUrl: string; state: string }>(
        `/api/platform-accounts/${encodeURIComponent(platform)}/authorize`,
        undefined,
      ),
    onSuccess: (data) => {
      window.location.assign(data.authorizeUrl)
    },
  })
}

export function useProbeAccount() {
  const refresh = useRefreshAccounts()
  return useMutation({
    mutationFn: (id: string) =>
      http.post<PlatformAccount>(`/api/platform-accounts/${encodeURIComponent(id)}/probe`, undefined),
    onSettled: refresh,
  })
}

/** 去登录: push the site's login page to the front of the cloud desktop. */
export function useOpenDesktopLogin() {
  const refresh = useRefreshAccounts()
  return useMutation({
    mutationFn: (site: string) =>
      http.post<PlatformAccount>(`/api/platform-accounts/desktop/${encodeURIComponent(site)}/open`, undefined),
    onSuccess: refresh,
  })
}

/** Cookie-level probe of every catalogued site on the workspace desktop. */
export function useProbeDesktopLogins() {
  const refresh = useRefreshAccounts()
  return useMutation({
    mutationFn: () => http.post<PlatformAccount[]>("/api/platform-accounts/desktop/probe", undefined),
    onSettled: refresh,
  })
}

/** 退出登录: delete that site's cookies on the desktop. */
export function useLogoutDesktopLogin() {
  const refresh = useRefreshAccounts()
  return useMutation({
    mutationFn: (id: string) =>
      http.post<PlatformAccount>(`/api/platform-accounts/${encodeURIComponent(id)}/logout`, undefined),
    onSuccess: refresh,
  })
}

export function useNotifications() {
  const userId = useUserId()
  const workspaceId = useWorkspaceId()
  return useQuery({
    queryKey: authCenterKeys.notifications(userId, workspaceId),
    queryFn: () => http.get<NotificationPage>("/api/notifications?unread=true&limit=20"),
    staleTime: 60_000,
  })
}

export function useMarkNotificationRead() {
  const qc = useQueryClient()
  const userId = useUserId()
  const workspaceId = useWorkspaceId()
  return useMutation({
    mutationFn: (id: string) => http.post(`/api/notifications/${encodeURIComponent(id)}/read`, undefined),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: authCenterKeys.notifications(userId, workspaceId) })
    },
  })
}

export function useUnbindAccount() {
  const refresh = useRefreshAccounts()
  return useMutation({
    mutationFn: (id: string) => http.delete(`/api/platform-accounts/${encodeURIComponent(id)}`),
    onSuccess: refresh,
  })
}

export interface PublishVars {
  fileAssetId: string
  title: string
  hashtags: string[]
  privateStatus: 0 | 1 | 2
}

export function usePublishToDouyin() {
  return useMutation({
    mutationFn: (vars: PublishVars) =>
      http.post<PublishResult>("/api/platform-accounts/douyin/publish", {
        file_asset_id: vars.fileAssetId,
        title: vars.title,
        hashtags: vars.hashtags,
        private_status: vars.privateStatus,
        download_type: 1,
      }),
  })
}

/** Poll one job while it is pending; the webhook flips it to published. */
export function usePublishJob(jobId: string | null, enabled: boolean) {
  const userId = useUserId()
  return useQuery({
    queryKey: authCenterKeys.job(userId, jobId ?? "none"),
    queryFn: () => http.get<PublishJob>(`/api/publish-jobs/${encodeURIComponent(jobId ?? "")}`),
    enabled: enabled && !!jobId,
    refetchInterval: (query) => (query.state.data?.status === "pending" ? 5_000 : false),
  })
}

/** Videos in the resource centre. Read through /api/assets directly so this
 *  feature does not reach into the resources feature. */
export function usePublishableVideos(enabled: boolean) {
  const userId = useUserId()
  const workspaceId = useWorkspaceId()
  return useQuery({
    queryKey: authCenterKeys.videos(userId, workspaceId),
    queryFn: async () => {
      const page = await http.get<{ items: VideoAsset[] }>(
        "/api/assets?project=all&source=all&kind=video&sort=created&limit=100",
      )
      return page.items
    },
    enabled,
    staleTime: 30_000,
  })
}
