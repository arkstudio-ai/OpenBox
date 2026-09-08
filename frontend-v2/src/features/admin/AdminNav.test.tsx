import { cleanup, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"
import { MemoryRouter } from "react-router"
import { AdminNav } from "./AdminNav"
import { activeAdminSection, ADMIN_SECTION_PATHS } from "./sections"

vi.mock("react-i18next", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react-i18next")>()
  const t = (key: string) => key
  return { ...actual, useTranslation: () => ({ t }) }
})

afterEach(cleanup)

describe("AdminNav", () => {
  it("links every column and marks only the active one", () => {
    render(
      <MemoryRouter>
        <AdminNav active="skills" />
      </MemoryRouter>,
    )
    const skills = screen.getByRole("link", { name: "nav.skills" })
    expect(skills.getAttribute("href")).toBe(ADMIN_SECTION_PATHS.skills)
    expect(skills.getAttribute("aria-current")).toBe("page")
    expect(screen.getByRole("link", { name: "nav.fleet" }).getAttribute("aria-current")).toBeNull()
    expect(screen.getByRole("link", { name: "nav.billing" }).getAttribute("aria-current")).toBeNull()
  })
})

describe("activeAdminSection", () => {
  it("maps each column path to its own column", () => {
    expect(activeAdminSection("/app/admin/fleet")).toBe("fleet")
    expect(activeAdminSection("/app/admin/skills")).toBe("skills")
    expect(activeAdminSection("/app/admin/skills/review")).toBe("skills")
    expect(activeAdminSection("/app/admin/billing/orders")).toBe("billing")
  })

  it("keeps the workspace detail under subscriptions", () => {
    expect(activeAdminSection("/app/admin/billing/workspaces/ws-1")).toBe("billing")
  })

  it("falls back to fleet, the way the index route redirects", () => {
    expect(activeAdminSection("/app/admin")).toBe("fleet")
    expect(activeAdminSection("/app/admin/")).toBe("fleet")
  })
})
