import { render, screen, cleanup } from "@testing-library/react"
import { MemoryRouter } from "react-router"
import { afterEach, expect, it, vi } from "vitest"
import { http } from "@/shared/api/http"
import { WikiPanel } from "./WikiPanel"
vi.mock("react-i18next", () => ({ useTranslation: () => ({ t: (key: string) => key }) }))
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})
it("opens the independent library in the selected project without starting compilation", () => {
  const post = vi.spyOn(http, "post")
  render(
    <MemoryRouter>
      <WikiPanel projectId="project-1" />
    </MemoryRouter>,
  )
  expect(screen.getByRole("link", { name: "openLibrary" }).getAttribute("href")).toBe(
    "/app/wiki?project=project-1",
  )
  expect(post).not.toHaveBeenCalled()
})
