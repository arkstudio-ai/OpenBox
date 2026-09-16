import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"
import type { PublishRouteStatus } from "../api/publish"

const mutate = vi.fn()
let status: PublishRouteStatus = { preference: null, deploymentDefault: "desktop", effective: "desktop", routes: ["desktop", "api"] }

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string, opts?: Record<string, unknown>) =>
      opts && typeof opts.route === "string" ? `${key}:${opts.route}` : key,
  }),
}))
vi.mock("../api/publish", () => ({
  usePublishRoute: () => ({ data: status }),
  useUpdatePublishRoute: () => ({ mutate, isPending: false }),
}))

import { PublishPage } from "./PublishPage"

afterEach(() => {
  cleanup()
  mutate.mockClear()
})

describe("PublishPage", () => {
  it("marks the deployment default as active when nothing is chosen, and stores a pick", () => {
    render(<PublishPage />)
    const desktop = screen.getByRole("button", { name: /publish.route.desktop/ })
    const api = screen.getByRole("button", { name: /publish.route.api/ })
    expect(desktop.getAttribute("aria-pressed")).toBe("true")
    expect(api.getAttribute("aria-pressed")).toBe("false")
    expect(screen.getByText("publish.defaultTag")).toBeTruthy()
    expect(screen.getByText("publish.followingDefault:publish.route.desktop")).toBeTruthy()
    expect(screen.queryByText("publish.resetToDefault")).toBeNull()
    fireEvent.click(api)
    expect(mutate).toHaveBeenCalledWith("api")
  })

  it("shows the person's own choice and lets them go back to the default", () => {
    status = { ...status, preference: "api", effective: "api" }
    render(<PublishPage />)
    expect(screen.getByRole("button", { name: /publish.route.api/ }).getAttribute("aria-pressed")).toBe("true")
    expect(screen.getByText("publish.chosen:publish.route.api")).toBeTruthy()
    fireEvent.click(screen.getByText("publish.resetToDefault"))
    expect(mutate).toHaveBeenCalledWith(null)
  })
})
