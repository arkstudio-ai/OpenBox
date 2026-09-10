import { useQuery } from "@tanstack/react-query"
import { http } from "@/shared/api/http"
import { useAuthStore } from "@/shared/api/auth-store"

export interface PushTest {
  id: string
  template: string
  title: string
  body: string
  status: "pending" | "sending" | "accepted" | "failed" | "cancelled" | "unbound"
  error: string | null
  receipt: "received" | "opened" | null
  receiptAt: string | null
  createdAt: string
  expiresAt: string
  availableAt: string | null
  attempts: number
}
export interface PushOverview {
  device: {
    registered: boolean
    bindingId: string | null
    platform: string | null
    provider: string | null
    appVersion: string | null
    apnsEnvironment: string | null
    notificationsEnabled: boolean
    ready: boolean
  }
  presence: { appState: string; pushAllowed: boolean }
  providers: { id: string; configured: boolean }[]
  templates: { id: string; title: string; body: string }[]
  tests: PushTest[]
  delaySeconds: number
  cooldownSeconds: number
}
export interface SendTest {
  template: string
  bindingId: string
  requestId: string
}
export function usePushOverview(locale: string) {
  const user = useAuthStore((s) => s.user)
  return useQuery({
    queryKey: ["admin-push", user?.id, locale],
    enabled: user?.role === "admin",
    retry: false,
    queryFn: ({ signal }) => http.get<PushOverview>(`/api/admin/push?locale=${locale}`, { signal }),
    refetchInterval: (query) =>
      query.state.data?.tests.some(
        (item) =>
          ["pending", "sending", "accepted"].includes(item.status) &&
          !item.receipt &&
          Date.parse(item.expiresAt) > Date.now(),
      )
        ? 2000
        : false,
  })
}
export const sendPushTest = (body: SendTest) => http.post<PushTest>("/api/admin/push/test", body)
