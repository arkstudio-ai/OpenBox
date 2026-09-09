import { afterEach, describe, expect, it, vi } from "vitest"
import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { MemoryRouter } from "react-router"
import { AppErrorBoundary } from "./AppErrorBoundary"
import { recoverChunkLoadError, reloadPage } from "@/shared/lib/chunk-recovery"

const routeError = vi.hoisted(() => ({
  value: new Error("Failed to fetch dynamically imported module: /assets/old.js"),
}))
vi.mock("react-router", async (original) => ({
  ...(await original<object>()),
  useRouteError: () => routeError.value,
}))
vi.mock("react-i18next", () => ({ useTranslation: () => ({ t: (key: string) => key }) }))
vi.mock("@/shared/lib/chunk-recovery", async (original) => ({
  ...(await original<object>()),
  recoverChunkLoadError: vi.fn(),
  reloadPage: vi.fn(),
}))
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.clearAllMocks()
})

describe("route error recovery UI", () => {
  it("explains stale resources and offers a real document reload", () => {
    vi.spyOn(console, "error").mockImplementation(() => {})
    routeError.value = new Error("Failed to fetch dynamically imported module: /assets/old.js")
    render(
      <MemoryRouter>
        <AppErrorBoundary />
      </MemoryRouter>,
    )
    expect(screen.getByRole("heading").textContent).toBe("pageLoad.title")
    expect(recoverChunkLoadError).toHaveBeenCalledWith(routeError.value)
    fireEvent.click(screen.getByRole("button", { name: "action.reload" }))
    expect(reloadPage).toHaveBeenCalledTimes(1)
    expect(screen.getByRole("link").getAttribute("href")).toBe("/app")
  })
  it("keeps ordinary errors distinct from a stale build", () => {
    vi.spyOn(console, "error").mockImplementation(() => {})
    routeError.value = new Error("Unexpected application failure")
    render(
      <MemoryRouter>
        <AppErrorBoundary />
      </MemoryRouter>,
    )
    expect(screen.getByRole("heading").textContent).toBe("state.error")
    expect(screen.getByText("pageLoad.genericBody")).toBeTruthy()
  })
})
