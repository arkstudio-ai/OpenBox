import { lazy, Suspense, useState } from "react"
import { createRoot } from "react-dom/client"
import { createBrowserRouter, RouterProvider } from "react-router"
import { AppErrorBoundary } from "../../src/app/router/AppErrorBoundary"
import "../../src/styles/index.css"
import "../../src/shared/i18n"

// The spec serves either HTML (the original production bug), 404, or JS.
const url = "/e2e/fixtures/recovery-target.js"
export const Route = lazy(() => import(/* @vite-ignore */ url))
export function Fixture() {
  const [open, setOpen] = useState(false)
  if (new URLSearchParams(location.search).has("ordinary-error")) throw new Error("HTTP 502")
  return open ? <Route /> : <button onClick={() => setOpen(true)}>打开授权中心</button>
}
const router = createBrowserRouter([{ path: "*", element: <Fixture />, errorElement: <AppErrorBoundary /> }])
createRoot(document.getElementById("root")!).render(
  <Suspense fallback={null}>
    <RouterProvider router={router} />
  </Suspense>,
)
