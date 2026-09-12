import { Suspense, useEffect } from "react"
import { Outlet, useMatch } from "react-router"
import { Sidebar, Topbar, useWorkspaceEvents, useWorkspaceUi } from "@/features/workspace"
import { DesktopActivationDialog, WorkbenchPanel, usePanelStore, usePanelEvents } from "@/features/workbench"
import { CronPanelTab, CronStatusPill } from "@/features/cron"
import { Spinner } from "@/shared/ui/Spinner"
import { useAuthStore } from "@/shared/api/auth-store"
import { useAppearanceStore } from "@/shared/appearance/store"
import { http } from "@/shared/api/http"
import type { UserPreferences } from "@/shared/types/api"
import { useWorkspacesQuery } from "@/shared/api/workspaces"
import { cn } from "@/shared/lib/cn"
import { paths, routePatterns } from "@/shared/router/paths"

/**
 * The viewer's own realtime channel. Opening `/ws/agent` is not passive: the
 * server provisions the viewer's sandbox on connect. It therefore mounts only
 * where the viewer works in their own sessions, and unmounting it (entering the
 * trajectory viewer) disconnects the socket.
 */
function ChatRealtime() {
  useWorkspaceEvents()
  usePanelEvents()
  return null
}

export default function WorkspaceLayout() {
  // Only a chat URL names the viewer's own active session. Other routes reuse
  // the `:sessionId` segment for other things — the admin trajectory viewer
  // puts *another user's* session there — and reading it blindly would file
  // that target as the viewer's last chat and hand it to the workbench,
  // desktop and cron widgets, which then call ordinary session APIs with it.
  const chatSessionId = useMatch(`${paths.app}/${routePatterns.chat}`)?.params.sessionId ?? null
  const panelOpen = usePanelStore((s) => s.open)
  const togglePanel = usePanelStore((s) => s.togglePanel)
  const userId = useAuthStore((s) => s.user?.id)
  const workspaces = useWorkspacesQuery()
  const isSettings = useMatch(`${paths.settings()}/*`) !== null
  const isBilling = useMatch(`${paths.billing()}/*`) !== null
  // The trajectory viewer is read-only observation of other people's work.
  // Nothing that acts for the viewer — agent socket, sandbox or desktop
  // activation, workbench panel, cron widget — may mount beside it.
  const isTrajectories = useMatch(`${paths.adminTrajectories()}/*`) !== null
  const setLastSession = useWorkspaceUi((s) => s.setLastSession)

  // The topbar's "back to chat" on centre pages returns here.
  useEffect(() => {
    if (chatSessionId) setLastSession(chatSessionId)
  }, [chatSessionId, setLastSession])

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

  const showWorkbench = !isBilling && !isTrajectories

  return (
    <div className="bg-bg text-ink flex h-screen overflow-hidden">
      {!isTrajectories && <ChatRealtime />}
      {/* The credit balance read settles the viewer's billing period server-side. */}
      <Sidebar showCredits={!isTrajectories} />
      {!isTrajectories && (
        <Suspense fallback={null}>
          <DesktopActivationDialog />
        </Suspense>
      )}
      <main
        className={cn(
          "flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden",
          !isSettings && !isBilling && "md:min-w-105",
        )}
      >
        <Topbar
          panelOpen={panelOpen}
          onTogglePanel={togglePanel}
          statusSlot={isTrajectories ? null : <CronStatusPill sessionId={chatSessionId} />}
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
      {showWorkbench && (
        <Suspense fallback={null}>
          <WorkbenchPanel sessionId={chatSessionId} cronTab={<CronPanelTab sessionId={chatSessionId} />} />
        </Suspense>
      )}
    </div>
  )
}
