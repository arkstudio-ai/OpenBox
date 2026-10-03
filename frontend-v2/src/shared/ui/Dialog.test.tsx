import { useState } from "react"
import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, expect, it, vi } from "vitest"
import { Dialog } from "./Dialog"
import { pushOverlay } from "./overlay-stack"

afterEach(cleanup)

function Opener({ field = false }: { field?: boolean }) {
  const [open, setOpen] = useState(false)
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        open
      </button>
      <button type="button">behind</button>
      <Dialog open={open} onClose={() => setOpen(false)} label="dialog">
        {field && <input aria-label="name" data-autofocus />}
        <button type="button">first</button>
        <button type="button" onClick={() => setOpen(false)}>
          last
        </button>
      </Dialog>
    </>
  )
}

it("moves focus in, keeps Tab inside and returns focus to the opener", () => {
  render(<Opener />)
  const opener = screen.getByRole("button", { name: "open" })
  opener.focus()
  fireEvent.click(opener)
  const dialog = screen.getByRole("dialog", { name: "dialog" })
  expect(document.activeElement).toBe(dialog)
  // Plain Tab from the dialog itself is the browser's: on to its first control.
  fireEvent.keyDown(window, { key: "Tab", shiftKey: true })
  expect(document.activeElement).toBe(screen.getByRole("button", { name: "last" }))
  fireEvent.keyDown(window, { key: "Tab" })
  expect(document.activeElement).toBe(screen.getByRole("button", { name: "first" }))
  fireEvent.keyDown(window, { key: "Tab", shiftKey: true })
  expect(document.activeElement).toBe(screen.getByRole("button", { name: "last" }))
  fireEvent.keyDown(window, { key: "Escape" })
  expect(screen.queryByRole("dialog")).toBeNull()
  expect(document.activeElement).toBe(opener)
})

it("starts at the field marked for focus", () => {
  render(<Opener field />)
  fireEvent.click(screen.getByRole("button", { name: "open" }))
  expect(document.activeElement).toBe(screen.getByLabelText("name"))
})

it("leaves Escape to an overlay opened on top of it", () => {
  const onClose = vi.fn()
  render(
    <Dialog open onClose={onClose} label="dialog">
      <button type="button">inside</button>
    </Dialog>,
  )
  const release = pushOverlay()
  fireEvent.keyDown(window, { key: "Escape" })
  expect(onClose).not.toHaveBeenCalled()
  release()
  fireEvent.keyDown(window, { key: "Escape" })
  expect(onClose).toHaveBeenCalledTimes(1)
})
