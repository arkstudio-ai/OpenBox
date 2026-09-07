// Browser-mode settings hooks. The agent drives either Chrome on the cloud
// desktop ("local") or the user's own Chrome via the extension ("remote");
// "auto" prefers remote and falls back to local when the extension is gone.
// Status is live, so the query re-checks reachability rather than trusting a
// cached value. Components never fetch directly (ENGINEERING_SPEC §7).
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { http } from "@/shared/api/http"
import { useAuthStore } from "@/shared/api/auth-store"
import { settingsKeys } from "./keys"

export type BrowserMode = "auto" | "local" | "remote"

export interface BrowserStatus {
  mode: BrowserMode
  preference: BrowserMode
  local: {
    available: boolean
    /** Why not, when unavailable: `unreachable` (the desktop itself did not
     *  answer), `not_started` (desktop fine, Chrome down), `no_sandbox`, `error`. */
    reason?: "unreachable" | "not_started" | "no_sandbox" | "error" | string
    /** The probe's own words, e.g. `connect: Connection refused`. */
    detail?: string
  }
  remote: { connected: boolean }
}

function useUserId(): string {
  return useAuthStore((s) => s.user?.id ?? "anonymous")
}

export function useBrowserStatus() {
  const userId = useUserId()
  return useQuery({
    queryKey: settingsKeys.browser(userId),
    queryFn: () => http.get<BrowserStatus>("/api/browser/status"),
    staleTime: 10_000,
  })
}

export function useUpdateBrowserPreference() {
  const userId = useUserId()
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (mode: BrowserMode) => http.put<BrowserStatus>("/api/browser/preference", { mode }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: settingsKeys.browser(userId) }),
  })
}
