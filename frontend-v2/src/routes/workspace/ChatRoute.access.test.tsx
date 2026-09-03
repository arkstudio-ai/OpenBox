import { cleanup, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"
import { ComposerAccess } from "./ChatRoute"

vi.mock("react-i18next", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react-i18next")>()
  const t = (key: string) => key
  return { ...actual, useTranslation: () => ({ t }) }
})

afterEach(cleanup)

describe("ComposerAccess", () => {
  it("does not render the composer for another member's session", () => {
    render(
      <ComposerAccess readOnly>
        <textarea aria-label="composer" />
      </ComposerAccess>,
    )
    expect(screen.queryByRole("textbox", { name: "composer" })).toBeNull()
    expect(screen.getByText("readOnlySession")).toBeTruthy()
  })

  it("renders the composer for the owner", () => {
    render(
      <ComposerAccess readOnly={false}>
        <textarea aria-label="composer" />
      </ComposerAccess>,
    )
    expect(screen.getByRole("textbox", { name: "composer" })).toBeTruthy()
  })
})
