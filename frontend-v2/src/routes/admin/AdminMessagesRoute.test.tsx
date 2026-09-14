import { cleanup, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"
import { MemoryRouter, Route, Routes } from "react-router"
import { paths, routePatterns } from "@/shared/router/paths"
import AdminMessagesRoute from "./AdminMessagesRoute"

vi.mock("react-i18next", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react-i18next")>()
  return { ...actual, useTranslation: () => ({ t: (key: string) => key }) }
})

vi.mock("@/features/admin-messages", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/features/admin-messages")>()),
  AnnouncementsPage: () => <div>page:announcements</div>,
  TopicsPage: () => <div>page:topics</div>,
}))

function renderAt(entry: string) {
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <Routes>
        <Route path={`${paths.admin}/${routePatterns.adminMessages}`} element={<AdminMessagesRoute />} />
      </Routes>
    </MemoryRouter>,
  )
}

afterEach(cleanup)

describe("AdminMessagesRoute", () => {
  it.each([
    [paths.adminMessages(), "page:announcements"],
    [paths.adminMessages("announcements"), "page:announcements"],
    [paths.adminMessages("topics"), "page:topics"],
    [paths.adminMessages("nonsense"), "page:announcements"],
  ])("%s renders %s", (entry, expected) => {
    renderAt(entry)
    expect(screen.getByText(expected)).toBeTruthy()
  })

  it("marks only the active tab", () => {
    renderAt(paths.adminMessages("topics"))
    expect(screen.getByRole("link", { name: "tab.topics" }).getAttribute("aria-current")).toBe("page")
    expect(screen.getByRole("link", { name: "tab.announcements" }).getAttribute("aria-current")).toBeNull()
  })
})
