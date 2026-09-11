import { cleanup, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"
import { MemoryRouter } from "react-router"
import { Bell } from "lucide-react"
import { NavRow } from "./NavRow"

afterEach(cleanup)

describe("NavRow badge", () => {
  it("shows the unread count only above zero and caps it at 99+", () => {
    const { rerender } = render(
      <MemoryRouter>
        <NavRow icon={Bell} label="消息中心" to="/app/inbox" badge={0} />
      </MemoryRouter>,
    )
    expect(screen.queryByTestId("nav-badge-/app/inbox")).toBeNull()
    rerender(
      <MemoryRouter>
        <NavRow icon={Bell} label="消息中心" to="/app/inbox" badge={4} />
      </MemoryRouter>,
    )
    expect(screen.getByTestId("nav-badge-/app/inbox").textContent).toBe("4")
    rerender(
      <MemoryRouter>
        <NavRow icon={Bell} label="消息中心" to="/app/inbox" badge={250} />
      </MemoryRouter>,
    )
    expect(screen.getByTestId("nav-badge-/app/inbox").textContent).toBe("99+")
  })

  it("lights up on its own route", () => {
    render(
      <MemoryRouter initialEntries={["/app/inbox"]}>
        <NavRow icon={Bell} label="消息中心" to="/app/inbox" />
      </MemoryRouter>,
    )
    expect(screen.getByRole("button", { name: "消息中心" }).getAttribute("aria-current")).toBe("page")
  })
})

vi.mock("react-i18next", async (importOriginal) => ({
  ...(await importOriginal<typeof import("react-i18next")>()),
}))
