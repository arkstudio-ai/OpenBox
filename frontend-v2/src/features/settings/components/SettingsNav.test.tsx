import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"
import { paths } from "@/shared/router/paths"
import { SETTINGS_TABS } from "@/features/settings/tabs"
import { SettingsNav } from "./SettingsNav"

const navigate = vi.fn()
vi.mock("react-router", () => ({ useNavigate: () => navigate }))
vi.mock("react-i18next", () => ({ useTranslation: () => ({ t: (key: string) => key }) }))

afterEach(() => {
  cleanup()
  navigate.mockClear()
})

describe("SettingsNav", () => {
  it("is a landmark holding every tab, with only the active one marked", () => {
    render(<SettingsNav active="models" />)
    const rail = screen.getByRole("navigation", { name: "title" })
    expect(screen.getAllByRole("button")).toHaveLength(SETTINGS_TABS.length)
    expect(screen.getByRole("button", { name: "nav.models" }).getAttribute("aria-current")).toBe("page")
    expect(screen.getByRole("button", { name: "nav.account" }).getAttribute("aria-current")).toBeNull()
    expect(rail.contains(screen.getByRole("button", { name: "nav.account" }))).toBe(true)
  })

  it("navigates to the tab that was clicked", () => {
    render(<SettingsNav active="account" />)
    fireEvent.click(screen.getByRole("button", { name: "nav.appearance" }))
    expect(navigate).toHaveBeenCalledWith(paths.settings("appearance"))
  })
})
