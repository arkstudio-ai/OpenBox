import { cleanup, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it } from "vitest"
import { MemoryRouter, Route, Routes } from "react-router"
import { useAuthStore } from "@/shared/api/auth-store"
import { paths } from "@/shared/router/paths"
import { RequireAdmin } from "./guards"

const ADMIN = { id: "u1", username: "root", role: "admin" }
const MEMBER = { id: "u2", username: "ann", role: "user" }

function renderGuard() {
  return render(
    <MemoryRouter initialEntries={[paths.admin]}>
      <Routes>
        <Route
          path={paths.admin}
          element={
            <RequireAdmin>
              <div>console</div>
            </RequireAdmin>
          }
        />
        <Route path={paths.app} element={<div>workspace</div>} />
      </Routes>
    </MemoryRouter>,
  )
}

afterEach(() => {
  cleanup()
  useAuthStore.setState({ user: null, isAuthenticated: false, isLoading: true })
})

describe("RequireAdmin", () => {
  it("renders the console for an admin", () => {
    useAuthStore.setState({ user: ADMIN, isAuthenticated: true, isLoading: false })
    renderGuard()
    expect(screen.getByText("console")).toBeTruthy()
  })

  it("sends a non-admin back to the workspace", () => {
    useAuthStore.setState({ user: MEMBER, isAuthenticated: true, isLoading: false })
    renderGuard()
    expect(screen.queryByText("console")).toBeNull()
    expect(screen.getByText("workspace")).toBeTruthy()
  })

  it("sends a signed-out visitor back to the workspace", () => {
    useAuthStore.setState({ user: null, isAuthenticated: false, isLoading: false })
    renderGuard()
    expect(screen.getByText("workspace")).toBeTruthy()
  })

  // A token refresh clears `user` for a tick; bouncing then would kick an admin
  // out of the console on every reload, so loading must hold the route.
  it("holds on the loader while the session is still settling", () => {
    useAuthStore.setState({ user: null, isAuthenticated: false, isLoading: true })
    renderGuard()
    expect(screen.getByRole("status")).toBeTruthy()
    expect(screen.queryByText("console")).toBeNull()
    expect(screen.queryByText("workspace")).toBeNull()
  })
})
