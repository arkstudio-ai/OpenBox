import { StrictMode } from "react"
import { createRoot } from "react-dom/client"
import { RouterProvider } from "react-router/dom"
import "@/styles/index.css"
import "@/shared/i18n"
import "@/shared/appearance/store"
import { refreshAccessToken } from "@/shared/api/auth-store"
import { installChunkRecovery } from "@/shared/lib/chunk-recovery"
import { watchBuild } from "@/shared/lib/build-version"
import { isAppBusy } from "@/shared/lib/activity"
import { AppProviders } from "@/app/providers/AppProviders"
import { router } from "@/app/router/router"

// Try restoring the session from the refresh cookie before first paint state
// settles; guards render a loader while this is in flight.
void refreshAccessToken()

// A deployment behind an open tab: missing chunks reload the tab once, and a
// newer build seen on the server swaps the tab at the next quiet moment
// (hidden, or on the next in-app navigation) rather than failing later.
installChunkRecovery()
const buildWatch = watchBuild({ isBusy: isAppBusy })
let lastLocationKey = router.state.location.key
router.subscribe((state) => {
  if (state.location.key === lastLocationKey) return
  lastLocationKey = state.location.key
  buildWatch.onNavigate()
})

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <AppProviders>
      <RouterProvider router={router} />
    </AppProviders>
  </StrictMode>,
)
