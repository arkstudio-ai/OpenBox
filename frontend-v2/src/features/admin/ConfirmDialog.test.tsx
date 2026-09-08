import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"
import { ConfirmDialog } from "./ConfirmDialog"

vi.mock("react-i18next", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react-i18next")>()
  const t = (key: string) => key
  return { ...actual, useTranslation: () => ({ t }) }
})

afterEach(cleanup)

describe("ConfirmDialog", () => {
  it("stays closed with nothing pending", () => {
    render(<ConfirmDialog pending={null} onClose={() => {}} />)
    expect(screen.queryByRole("dialog")).toBeNull()
  })

  it("runs the action only once the operator confirms", () => {
    const run = vi.fn()
    const onClose = vi.fn()
    render(<ConfirmDialog pending={{ body: "release?", run }} onClose={onClose} />)
    expect(screen.getByText("release?")).toBeTruthy()

    fireEvent.click(screen.getByText("common:action.cancel"))
    expect(run).not.toHaveBeenCalled()
    expect(onClose).toHaveBeenCalledTimes(1)

    fireEvent.click(screen.getByText("common:action.confirm"))
    expect(run).toHaveBeenCalledTimes(1)
    expect(onClose).toHaveBeenCalledTimes(2)
  })
})
