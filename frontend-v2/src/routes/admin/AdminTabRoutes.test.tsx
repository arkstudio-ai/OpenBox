// The two tab dispatchers are the seam between the console shell and the
// feature pages: `:tab?` is optional and hand-editable, so both a missing and a
// nonsense segment have to land somewhere real rather than render nothing.
import { cleanup, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"
import { MemoryRouter, Route, Routes } from "react-router"
import { paths, routePatterns } from "@/shared/router/paths"
import AdminSkillsRoute from "./AdminSkillsRoute"
import AdminBillingRoute from "./AdminBillingRoute"

vi.mock("react-i18next", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react-i18next")>()
  return { ...actual, useTranslation: () => ({ t: (key: string) => key }) }
})

// The pages themselves are covered by their own tests and each opens a query;
// stubbing them keeps this about which page the URL picks.
vi.mock("@/features/admin-skills", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/features/admin-skills")>()),
  StorePage: () => <div>page:store</div>,
  ReviewPage: () => <div>page:review</div>,
  InstallsPage: () => <div>page:installs</div>,
}))

vi.mock("@/features/admin-billing", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/features/admin-billing")>()),
  SubscriptionsPage: () => <div>page:subscriptions</div>,
  OrdersPage: () => <div>page:orders</div>,
}))

function renderAt(entry: string, pattern: string, element: React.ReactNode) {
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <Routes>
        <Route path={`${paths.admin}/${pattern}`} element={element} />
      </Routes>
    </MemoryRouter>,
  )
}

afterEach(cleanup)

describe("AdminSkillsRoute", () => {
  it.each([
    [paths.adminSkills(), "page:store"],
    [paths.adminSkills("store"), "page:store"],
    [paths.adminSkills("review"), "page:review"],
    [paths.adminSkills("installs"), "page:installs"],
    // A stale bookmark must not leave the column blank.
    [paths.adminSkills("nonsense"), "page:store"],
  ])("%s renders %s", (entry, expected) => {
    renderAt(entry, routePatterns.adminSkills, <AdminSkillsRoute />)
    expect(screen.getByText(expected)).toBeTruthy()
  })

  it("marks only the active tab and links the others", () => {
    renderAt(paths.adminSkills("review"), routePatterns.adminSkills, <AdminSkillsRoute />)
    expect(screen.getByRole("link", { name: "tab.review" }).getAttribute("aria-current")).toBe("page")
    const store = screen.getByRole("link", { name: "tab.store" })
    expect(store.getAttribute("aria-current")).toBeNull()
    expect(store.getAttribute("href")).toBe(paths.adminSkills("store"))
  })
})

describe("AdminBillingRoute", () => {
  it.each([
    [paths.adminBilling(), "page:subscriptions"],
    [paths.adminBilling("subscriptions"), "page:subscriptions"],
    [paths.adminBilling("orders"), "page:orders"],
    [paths.adminBilling("nonsense"), "page:subscriptions"],
  ])("%s renders %s", (entry, expected) => {
    renderAt(entry, routePatterns.adminBilling, <AdminBillingRoute />)
    expect(screen.getByText(expected)).toBeTruthy()
  })

  it("marks only the active tab and links the other", () => {
    renderAt(paths.adminBilling("orders"), routePatterns.adminBilling, <AdminBillingRoute />)
    expect(screen.getByRole("link", { name: "tabs.orders" }).getAttribute("aria-current")).toBe("page")
    const subscriptions = screen.getByRole("link", { name: "tabs.subscriptions" })
    expect(subscriptions.getAttribute("aria-current")).toBeNull()
    expect(subscriptions.getAttribute("href")).toBe(paths.adminBilling("subscriptions"))
  })
})
