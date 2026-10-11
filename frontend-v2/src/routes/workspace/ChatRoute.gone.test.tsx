import { cleanup, render, screen } from "@testing-library/react"
import { MemoryRouter, Route, Routes } from "react-router"
import { afterEach, describe, expect, it, vi } from "vitest"
import { ApiError } from "@/shared/api/http"
import { toast } from "@/shared/ui/Toast"
import { GoneSession, isGoneSession } from "./ChatRoute"

vi.mock("react-i18next", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react-i18next")>()
  const t = (key: string) => key
  return { ...actual, useTranslation: () => ({ t }) }
})
vi.mock("@/shared/ui/Toast", () => ({ toast: vi.fn() }))

afterEach(cleanup)

describe("a conversation that no longer opens", () => {
  it("treats 404 and 410 as gone, everything else as a real error", () => {
    expect(isGoneSession(new ApiError(404, "SESSION_NOT_FOUND", "gone"))).toBe(true)
    expect(isGoneSession(new ApiError(410, "SESSION_DELETED", "gone"))).toBe(true)
    expect(isGoneSession(new ApiError(500, "HTTP_500", "boom"))).toBe(false)
    expect(isGoneSession(new Error("network"))).toBe(false)
    expect(isGoneSession(null)).toBe(false)
  })

  it("goes back to the workspace home and says why", () => {
    render(
      <MemoryRouter initialEntries={["/app/s/session_missing"]}>
        <Routes>
          <Route path="/app/s/:sessionId" element={<GoneSession />} />
          <Route path="/app" element={<p>home</p>} />
        </Routes>
      </MemoryRouter>,
    )
    expect(screen.getByText("home")).toBeTruthy()
    expect(toast).toHaveBeenCalledWith("info", "pageLoad.sessionGone")
  })
})
