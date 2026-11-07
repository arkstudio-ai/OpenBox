// When a click on the dimmed page closes the sheet, and when it must not:
// a drag that starts inside (selecting text) and ends outside is not a dismissal,
// and a form sheet can turn backdrop dismissal off while Escape still works.
import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"
import { Sheet } from "./Sheet"

afterEach(cleanup)

function renderSheet(dismissOnBackdrop?: boolean) {
  const onClose = vi.fn()
  render(
    <Sheet open onClose={onClose} title="抽屉" closeLabel="关闭" dismissOnBackdrop={dismissOnBackdrop}>
      <p>正文</p>
    </Sheet>,
  )
  const dialog = screen.getByRole("dialog")
  const backdrop = dialog.parentElement!
  return { onClose, dialog, backdrop }
}

describe("Sheet backdrop", () => {
  it("closes on a click that starts and ends on the backdrop", () => {
    const { onClose, backdrop } = renderSheet()
    fireEvent.pointerDown(backdrop)
    fireEvent.click(backdrop)
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it("keeps open when a drag starts inside the panel and releases on the backdrop", () => {
    const { onClose, backdrop } = renderSheet()
    fireEvent.pointerDown(screen.getByText("正文"))
    // The browser dispatches the click on the common ancestor: the backdrop.
    fireEvent.click(backdrop)
    expect(onClose).not.toHaveBeenCalled()
  })

  it("never closes on the backdrop when dismissal is off, but Escape and the button still do", () => {
    const { onClose, backdrop } = renderSheet(false)
    fireEvent.pointerDown(backdrop)
    fireEvent.click(backdrop)
    expect(onClose).not.toHaveBeenCalled()
    fireEvent.keyDown(window, { key: "Escape" })
    expect(onClose).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getByRole("button", { name: "关闭" }))
    expect(onClose).toHaveBeenCalledTimes(2)
  })
})
