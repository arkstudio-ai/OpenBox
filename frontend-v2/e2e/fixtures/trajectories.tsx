// Fixture shell for the admin trajectory viewer. It mounts the production
// route components — list and session, each with its access watcher — under
// the app's own providers and StrictMode, signs in an isolated fixture admin
// with a placeholder token, and leaves every request to the Playwright-side
// fake server (e2e/helpers/trajectory-server.ts). It renders no trajectory
// content of its own.
//
//   /e2e/fixtures/trajectories.html?lang=en-US&mode=dark#/app/admin/trajectories/sessions/<id>?at=16
import { StrictMode, useEffect } from "react"
import { createRoot } from "react-dom/client"
import { MemoryRouter, Route, Routes, useLocation } from "react-router"
import { AppProviders } from "../../src/app/providers/AppProviders"
import AdminTrajectoriesRoute from "../../src/routes/admin/AdminTrajectoriesRoute"
import AdminTrajectorySessionRoute from "../../src/routes/admin/AdminTrajectorySessionRoute"
import { useAuthStore } from "../../src/shared/api/auth-store"
import i18n from "../../src/shared/i18n"
import "../../src/styles/index.css"

type AuthUser = NonNullable<ReturnType<typeof useAuthStore.getState>["user"]>

interface FixtureControls {
  /** The same account with another role, as an account refresh would report after a demotion. */
  setRole: (role: string) => void
  signOut: () => void
}

declare global {
  interface Window {
    __trajectoryFixture?: FixtureControls
  }
}

const search = new URLSearchParams(window.location.search)
const language = search.get("lang") === "en-US" ? "en-US" : "zh-CN"
await i18n.changeLanguage(language)
document.documentElement.lang = language
if (search.get("mode") === "dark") document.documentElement.setAttribute("data-mode", "dark")

useAuthStore.setState({
  // Not a JWT: the fake server never inspects it and no real backend sees it.
  accessToken: "fixture-placeholder-token",
  user: { id: "fixture-admin", username: "fixture-admin", role: "admin" } as AuthUser,
  isAuthenticated: true,
  isLoading: false,
})

window.__trajectoryFixture = {
  setRole: (role) => {
    const user = useAuthStore.getState().user
    if (user) useAuthStore.setState({ user: { ...user, role } as AuthUser })
  },
  signOut: () => useAuthStore.getState().clearAuth(),
}

function LocationProbe() {
  const location = useLocation()
  const href = `${location.pathname}${location.search}`
  useEffect(() => {
    // Mirror the in-app route into the hash so a reload reopens the same view.
    window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}#${href}`)
  }, [href])
  return (
    <output data-testid="fixture-location" hidden>
      {href}
    </output>
  )
}

const initial = window.location.hash.slice(1) || "/app/admin/trajectories"

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <AppProviders>
      <MemoryRouter initialEntries={[initial]}>
        <main className="bg-bg text-ink min-h-dvh min-w-0 px-3 py-4 sm:px-6">
          <Routes>
            <Route path="/app/admin/trajectories" element={<AdminTrajectoriesRoute />} />
            <Route
              path="/app/admin/trajectories/sessions/:sessionId"
              element={<AdminTrajectorySessionRoute />}
            />
          </Routes>
          <LocationProbe />
        </main>
      </MemoryRouter>
    </AppProviders>
  </StrictMode>,
)
