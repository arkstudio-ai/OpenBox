import { Suspense, useEffect } from "react"
import { Outlet, useMatch } from "react-router"
import { Sidebar, Topbar, useWorkspaceEvents, useWorkspaceUi } from "@/features/workspace"
import { DesktopActivationDialog, WorkbenchPanel, usePanelStore, usePanelEvents } from "@/features/workbench"
import { CronStatusPill } from "@/features/cron"
import { MemoryPauseToggle } from "@/features/memory"
import { useInboxLiveEvents } from "@/features/inbox"
import { AssistantTopbarActions, useAssistantSidebarUnread, useSessionQuery } from "@/features/chat"
import { VoiceCallButton, VoiceCallDock } from "@/features/voice"
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
function ChatRealtime({ surface }: { surface: "assistant" | "workspace" }) {
  useWorkspaceEvents(surface)
  useInboxLiveEvents()
  usePanelEvents()
  return null
}

function useWorkspaceSurface() {
  // Only a chat URL names the viewer's own active session. Other routes reuse
  // the `:sessionId` segment for other things — the admin trajectory viewer
  // puts *another user's* session there — and reading it blindly would file
  // that target as the viewer's last chat and hand it to the workbench,
  // desktop and cron widgets, which then call ordinary session APIs with it.
  const chatSessionId = useMatch(`${paths.app}/${routePatterns.chat}`)?.params.sessionId ?? null
  const chatSession = useSessionQuery(chatSessionId ?? "")
  // The personal assistant's own conversation has no sandbox or desktop of its
  // own. Conversations it hands work to are ordinary project conversations:
  // they use the workspace cloud desktop like any other.
  const isAssistant = useMatch(paths.assistant) !== null || chatSession.data?.kind === "assistant"
  const isSettings = useMatch(`${paths.settings()}/*`) !== null
  const isAdmin = useMatch(`${paths.admin}/*`) !== null
  const isBilling = useMatch(`${paths.billing()}/*`) !== null
  // The desktop page streams the desktop itself; a panel beside it would open
  // a second connection to the same machine.
  const isDesktopPage = useMatch(paths.desktop) !== null
  // The trajectory viewer is read-only observation of other people's work.
  // Nothing that acts for the viewer — agent socket, sandbox or desktop
  // activation, workbench panel, cron widget — may mount beside it.
  const isTrajectories = useMatch(`${paths.adminTrajectories()}/*`) !== null
  // Merely inspecting memory or its diagnostics must not provision a sandbox,
  // settle billing, or start any background model work through the chat socket.
  const isMemoryPage = useMatch(paths.memory) !== null
  const isMemoryDebug = useMatch(`${paths.memoryDebug()}/*`) !== null
  const isWiki = useMatch(`${paths.wiki()}/*`) !== null
  const isObservation = isTrajectories || isMemoryPage || isMemoryDebug || isWiki
  const ownSessionReady = !chatSessionId || (!!chatSession.data && !chatSession.error)
  return { chatSessionId, isAssistant, isSettings, isAdmin, isBilling, isObservation, isDesktopPage,
    ownSessionReady, assistantBadgeVisible: ownSessionReady && !isObservation && !isSettings && !isAdmin }
}

export default function WorkspaceLayout() {
  const { chatSessionId, isAssistant, isSettings, isAdmin, isBilling, isObservation, isDesktopPage, ownSessionReady, assistantBadgeVisible } = useWorkspaceSurface()
  const assistantUnread = useAssistantSidebarUnread(isAssistant, assistantBadgeVisible)
  const panelOpen = usePanelStore((s) => s.open)
  const developerMode = useAppearanceStore((s) => s.developerMode)
  // Without developer mode the panel has one thing to show, so the toggle
  // opens the cloud desktop straight away rather than a menu of tabs.
  const togglePanel = () => {
    const panel = usePanelStore.getState()
    if (panel.open || developerMode) panel.togglePanel()
    else panel.openKind("desktop")
  }
  const userId = useAuthStore((s) => s.user?.id)
  const workspaces = useWorkspacesQuery()
  const setLastSession = useWorkspaceUi((s) => s.setLastSession)

  // Settings and the admin console take the whole window: their own nav rail is
  // the only one on screen, and the topbar's back link is the way out. Two rails
  // side by side made the workspace sidebar and the page's own rail compete to
  // answer "where am I", and neither page is somewhere you browse alongside a
  // conversation — you go in, change something, and come back.
  const takeover = isSettings || isAdmin

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

  // The panel belongs to a conversation, and the topbar already refuses to open
  // it away from one. Left mounted it would reappear beside a takeover page as a
  // third column — the very thing the takeover removes.
  const showWorkbench = ownSessionReady && !isBilling && !isObservation && !takeover && !isDesktopPage && !isAssistant

  return (
    <div className="bg-bg text-ink flex h-screen overflow-hidden">
      {!isObservation && ownSessionReady && <ChatRealtime surface={isAssistant ? "assistant" : "workspace"} />}
      {/* The credit balance read settles the viewer's billing period server-side,
          so the trajectory viewer keeps opting out even though a takeover page
          renders no sidebar at all today. */}
      {!takeover && (
        <Sidebar assistantUnread={assistantUnread} showCredits={!isObservation} />
      )}
      {!isObservation && !isAssistant && ownSessionReady && (
        <Suspense fallback={null}>
          <DesktopActivationDialog />
        </Suspense>
      )}
      <main
        className={cn(
          "flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden",
          !takeover && !isBilling && "md:min-w-105",
        )}
      >
        <Topbar
          panelOpen={panelOpen}
          onTogglePanel={togglePanel}
          actions={
            // Own boundaries: the voice and chat namespaces may still be loading.
            isAssistant && (
              <>
                <Suspense fallback={null}>
                  <VoiceCallButton />
                </Suspense>
                <Suspense fallback={null}>
                  <AssistantTopbarActions />
                </Suspense>
              </>
            )
          }
          statusSlot={
            isObservation || isAssistant ? null : (
              <>
                <MemoryPauseToggle sessionId={chatSessionId} />
                <CronStatusPill sessionId={chatSessionId} />
              </>
            )
          }
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
          <WorkbenchPanel sessionId={chatSessionId} developerMode={developerMode} />
        </Suspense>
      )}
      {/* On every page of the shell, takeover pages included: a call started
          from the assistant goes on while the user looks elsewhere. */}
      <Suspense fallback={null}>
        <VoiceCallDock />
      </Suspense>
    </div>
  )
}
