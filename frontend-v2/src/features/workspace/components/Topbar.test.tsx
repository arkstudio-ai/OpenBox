// Settings and the admin console take the window over: the workspace sidebar is
// gone, so the topbar owns the only way back. These cover where that exit sits,
// and that Escape reaches it without stealing the key from anything nearer.
import type { AnchorHTMLAttributes, ReactNode } from "react"
import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"
import { paths } from "@/shared/router/paths"
import { pushOverlay } from "@/shared/ui/overlay-stack"
import type { Session } from "@/shared/types/api"
import type { StandalonePage } from "../lib/standalonePage"
import { useWorkspaceUi } from "../stores/ui"
import { Topbar } from "./Topbar"

const navigate = vi.fn()
vi.mock("react-router", () => ({
  useNavigate: () => navigate,
  Link: ({
    to,
    children,
    ...rest
  }: { to: string; children: ReactNode } & Omit<AnchorHTMLAttributes<HTMLAnchorElement>, "href">) => (
    <a href={to} {...rest}>
      {children}
    </a>
  ),
}))
vi.mock("react-i18next", () => ({ useTranslation: () => ({ t: (key: string) => key }) }))
vi.mock("@/shared/ui/EnvBadge", () => ({ EnvBadge: () => null }))
vi.mock("@/shared/hooks/useCopy", () => ({ useCopy: () => ({ copy: vi.fn() }) }))

const heading = vi.hoisted(() => ({
  page: null as StandalonePage | null,
  title: "title",
  subtitle: "subtitle",
}))
vi.mock("../hooks/useTopbarHeading", () => ({
  useTopbarHeading: () => ({ ...heading, session: null }),
}))

const sessions = vi.hoisted(() => ({ data: [] as Pick<Session, "id">[] }))
vi.mock("../api/sessions", () => ({ useSessionsQuery: () => sessions }))

// Collapsed, so the expand affordance would render on any page that still has
// a sidebar to expand.
vi.mock("../hooks/useSidebarLayout", () => ({
  useSidebarLayout: () => ({ compact: false, open: false, toggle: vi.fn(), close: vi.fn() }),
}))

function mount(page: StandalonePage | null) {
  heading.page = page
  return render(<Topbar panelOpen={false} onTogglePanel={vi.fn()} statusSlot={null} />)
}

const backLink = () => screen.getByRole("link", { name: "backToChat" })

afterEach(() => {
  cleanup()
  navigate.mockClear()
  sessions.data = []
  heading.subtitle = "subtitle"
  useWorkspaceUi.setState({ lastSessionId: null })
})

describe("Topbar on a takeover page", () => {
  it.each<StandalonePage>(["settings", "admin"])("puts the way out first on %s", (page) => {
    const { container } = mount(page)
    const bar = container.firstElementChild!
    expect(bar.firstElementChild).toBe(backLink())
    // Nothing to expand: the sidebar is not rendered beside these pages.
    expect(screen.queryByRole("button", { name: "expand" })).toBeNull()
    // The page names itself in its own rail, so the chrome repeats neither.
    expect(screen.queryByText("title")).toBeNull()
    expect(screen.queryByText("subtitle")).toBeNull()
  })

  it("returns to the conversation the person left", () => {
    sessions.data = [{ id: "s1" }]
    useWorkspaceUi.setState({ lastSessionId: "s1" })
    mount("settings")
    expect(backLink().getAttribute("href")).toBe(paths.chat("s1"))
  })

  it("falls back to a fresh chat when that conversation is gone", () => {
    useWorkspaceUi.setState({ lastSessionId: "deleted" })
    mount("settings")
    expect(backLink().getAttribute("href")).toBe(paths.newChat())
  })

  it("leaves on Escape", () => {
    mount("admin")
    fireEvent.keyDown(window, { key: "Escape" })
    expect(navigate).toHaveBeenCalledWith(paths.newChat())
  })

  it("yields Escape to an open dialog, menu or drawer", () => {
    mount("settings")
    const release = pushOverlay()
    fireEvent.keyDown(window, { key: "Escape" })
    expect(navigate).not.toHaveBeenCalled()
    // ...and takes it back once that overlay closes.
    release()
    fireEvent.keyDown(window, { key: "Escape" })
    expect(navigate).toHaveBeenCalledTimes(1)
  })

  it("yields Escape to a field being edited", () => {
    const { container } = mount("settings")
    const input = document.createElement("input")
    container.appendChild(input)
    fireEvent.keyDown(input, { key: "Escape" })
    expect(navigate).not.toHaveBeenCalled()
  })

  it("ignores an Escape something else already handled", () => {
    mount("settings")
    const event = new KeyboardEvent("keydown", { key: "Escape", cancelable: true })
    event.preventDefault()
    window.dispatchEvent(event)
    expect(navigate).not.toHaveBeenCalled()
  })
})

describe("Topbar elsewhere", () => {
  it("keeps the sidebar affordance and the trailing exit on an ordinary centre page", () => {
    const { container } = mount("cron")
    const bar = container.firstElementChild!
    expect(bar.firstElementChild).toBe(screen.getByRole("button", { name: "expand" }))
    expect(backLink()).toBeTruthy()
    expect(screen.getByText("title")).toBeTruthy()
    expect(screen.getByText("subtitle")).toBeTruthy()
  })

  it("does not take Escape away from a centre page that never took the window over", () => {
    mount("cron")
    fireEvent.keyDown(window, { key: "Escape" })
    expect(navigate).not.toHaveBeenCalled()
  })

  it("offers no way back from the chat surface itself", () => {
    mount(null)
    expect(screen.queryByRole("link", { name: "backToChat" })).toBeNull()
  })
})
