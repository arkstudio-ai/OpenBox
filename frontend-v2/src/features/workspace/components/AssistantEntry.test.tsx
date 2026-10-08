import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"
import { MemoryRouter, Route, Routes } from "react-router"
import { AssistantEntry } from "./AssistantEntry"

vi.mock("react-i18next", async (original) => ({
  ...await original<typeof import("react-i18next")>(),
  useTranslation: () => ({ t: (key: string) => key }),
}))

afterEach(cleanup)

describe("AssistantEntry", () => {
  it("says what the assistant is for and opens its page", () => {
    render(
      <MemoryRouter initialEntries={["/app"]}>
        <Routes>
          <Route path="/app" element={<AssistantEntry />} />
          <Route path="/app/assistant" element={<p>assistant page</p>} />
        </Routes>
      </MemoryRouter>,
    )
    const entry = screen.getByRole("button", { name: /assistant/ })
    expect(entry.textContent).toContain("assistantTagline")
    expect(entry.getAttribute("title")).toBe("assistantHint")
    expect(screen.queryByTestId("assistant-unread")).toBeNull()
    fireEvent.click(entry)
    expect(screen.getByText("assistant page")).toBeTruthy()
  })

  it("carries the unread count, capped, and lights up on its page", () => {
    const { rerender } = render(
      <MemoryRouter initialEntries={["/app/assistant"]}>
        <AssistantEntry unread={{ count: 3, lowerBound: true }} />
      </MemoryRouter>,
    )
    expect(screen.getByTestId("assistant-unread").textContent).toBe("3+")
    expect(screen.getByRole("button", { name: /assistant/ }).getAttribute("aria-current")).toBe("page")
    rerender(
      <MemoryRouter initialEntries={["/app/assistant"]}>
        <AssistantEntry unread={{ count: 120, lowerBound: false }} />
      </MemoryRouter>,
    )
    expect(screen.getByTestId("assistant-unread").textContent).toBe("99+")
  })
})
