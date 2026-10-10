import { cleanup, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it } from "vitest"
import { MemoryRouter } from "react-router"
import { Bell, Clock } from "lucide-react"
import { NavTile } from "./NavTile"

afterEach(cleanup)

describe("NavTile", () => {
  it("is named by its label, explains itself on hover and caps the unread count", () => {
    const { rerender } = render(
      <MemoryRouter>
        <NavTile icon={Bell} label="消息中心" hint="会话结果与官方通知" to="/app/inbox" badge={0} />
      </MemoryRouter>,
    )
    const tile = screen.getByRole("button", { name: "消息中心" })
    expect(tile.getAttribute("title")).toBe("消息中心 · 会话结果与官方通知")
    expect(screen.queryByTestId("nav-badge-/app/inbox")).toBeNull()
    rerender(
      <MemoryRouter>
        <NavTile icon={Bell} label="消息中心" to="/app/inbox" badge={250} />
      </MemoryRouter>,
    )
    expect(screen.getByTestId("nav-badge-/app/inbox").textContent).toBe("99+")
  })

  it("lights up on its page and on the pages under it", () => {
    render(
      <MemoryRouter initialEntries={["/app/cron/job-1"]}>
        <NavTile icon={Clock} label="定时任务" to="/app/cron" pattern="/app/cron/*" />
      </MemoryRouter>,
    )
    expect(screen.getByRole("button", { name: "定时任务" }).getAttribute("aria-current")).toBe("page")
  })
})
