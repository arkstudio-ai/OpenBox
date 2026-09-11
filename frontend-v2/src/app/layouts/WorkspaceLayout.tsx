import { Suspense, useEffect } from "react"
import { Outlet, useMatch, useParams } from "react-router"
import { Sidebar, Topbar, useWorkspaceEvents, useWorkspaceUi } from "@/features/workspace"
import { DesktopActivationDialog, WorkbenchPanel, usePanelStore, usePanelEvents } from "@/features/workbench"
import { CronPanelTab, CronStatusPill } from "@/features/cron"
import { useInboxLiveEvents } from "@/features/inbox"
import { Spinner } from "@/shared/ui/Spinner"
import { useAuthStore } from "@/shared/api/auth-store"
import { useAppearanceStore } from "@/shared/appearance/store"
import { http } from "@/shared/api/http"
import type { UserPreferences } from "@/shared/types/api"
import { useWorkspacesQuery } from "@/shared/api/workspaces"
import { cn } from "@/shared/lib/cn"
import { paths } from "@/shared/router/paths"

export default function WorkspaceLayout() {
  useWorkspaceEvents()
  useInboxLiveEvents()
  usePanelEvents()
  const { sessionId } = useParams()
  const panelOpen = usePanelStore((s) => s.open)
  const togglePanel = usePanelStore((s) => s.togglePanel)
  const userId = useAuthStore((s) => s.user?.id)
  const workspaces = useWorkspacesQuery()
  const isSettings = useMatch(`${paths.settings()}/*`) !== null
  const isBilling = useMatch(`${paths.billing()}/*`) !== null
  const setLastSession = useWorkspaceUi((s) => s.setLastSession)

  // The topbar's "back to chat" on centre pages returns here.
  useEffect(() => {
    if (sessionId) setLastSession(sessionId)
  }, [sessionId, setLastSession])

  // Hydrate appearance from server prefs once per signed-in user.
  useEffect(() => {
    if (!userId) return
    void http
      .get<UserPreferences>("/api/auth/me/preferences")
      .then((prefs) => useAppearanceStore.getState().hydrateFromServer(prefs))
      .catch(() => undefined)
  }, [userId])

  if (workspaces.error) throw workspaces.error
  if (!workspaces.data) {
    return (
      <div className="bg-bg flex h-screen items-center justify-center">
        <Spinner className="size-6" />
      </div>
    )
  }

  return (
    <div className="bg-bg text-ink flex h-screen overflow-hidden">
      <Sidebar />
      <Suspense fallback={null}>
        <DesktopActivationDialog />
      </Suspense>
      <main
        className={cn(
          "flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden",
          !isSettings && !isBilling && "md:min-w-105",
        )}
      >
        <Topbar
          panelOpen={panelOpen}
          onTogglePanel={togglePanel}
          statusSlot={<CronStatusPill sessionId={sessionId ?? null} />}
        />
        <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
          <Suspense
            fallback={
              <div className="flex flex-1 items-center justify-center">
                <Spinner className="size-5" />
              </div>
            }
          >
            <Outlet />
          </Suspense>
        </div>
      </main>
      {/* Own boundary: the panel loads its i18n namespace on first open, and
          without this that suspension escapes to the router boundary and blanks
          the whole workspace. */}
      {!isBilling && (
        <Suspense fallback={null}>
          <WorkbenchPanel
            sessionId={sessionId ?? null}
            cronTab={<CronPanelTab sessionId={sessionId ?? null} />}
          />
        </Suspense>
      )}
    </div>
  )
}
