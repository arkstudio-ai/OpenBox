import { type ReactNode } from "react"
import { Navigate, useLocation } from "react-router"
import { useAuthStore } from "@/shared/api/auth-store"
import { FullScreenLoader } from "@/app/providers/AppProviders"
import { paths } from "@/app/router/paths"

export function RequireAuth({ children }: { children: ReactNode }) {
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated)
  const isLoading = useAuthStore((s) => s.isLoading)
  const location = useLocation()

  if (isLoading) return <FullScreenLoader />
  if (!isAuthenticated) {
    return <Navigate to={paths.login} state={{ from: location.pathname }} replace />
  }
  return children
}

/**
 * Role gate for the admin console. Nests inside `RequireAuth`, so by the time
 * it runs the session is settled — but it still honours `isLoading` because a
 * token refresh clears `user` for a tick and would otherwise bounce an admin
 * back to the workspace mid-refresh.
 */
export function RequireAdmin({ children }: { children: ReactNode }) {
  const role = useAuthStore((s) => s.user?.role)
  const isLoading = useAuthStore((s) => s.isLoading)
  if (isLoading) return <FullScreenLoader />
  if (role !== "admin") return <Navigate to={paths.app} replace />
  return children
}

export function RedirectIfAuthed({ children }: { children: ReactNode }) {
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated)
  const isLoading = useAuthStore((s) => s.isLoading)
  if (isLoading) return <FullScreenLoader />
  if (isAuthenticated) return <Navigate to={paths.app} replace />
  return children
}
