import { act, cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { SearchBox } from "./SearchBox"

function mount(onChange: (value: string) => void, value = "") {
  return render(<SearchBox value={value} onChange={onChange} label="搜索商店条目" placeholder="搜索" />)
}

const box = () => screen.getByRole("searchbox", { name: "搜索商店条目" })
const type = (text: string) => fireEvent.change(box(), { target: { value: text } })
const wait = (ms: number) => act(() => void vi.advanceTimersByTime(ms))

beforeEach(() => vi.useFakeTimers())

afterEach(() => {
  vi.useRealTimers()
  cleanup()
})

describe("SearchBox", () => {
  // Every committed value is a request and an `admin.view_skills` audit row
  // carrying `q` (plan §4.7). A live box turns one typed word into ten of each,
  // which is how the skill-view audit trail becomes a transcript of typing.
  it("commits the typed word once, not once per keystroke", () => {
    const onChange = vi.fn()
    mount(onChange)
    for (const prefix of ["p", "pl", "pla", "play", "playw", "playwright"]) {
      type(prefix)
      wait(100)
    }
    expect(onChange).not.toHaveBeenCalled()

    wait(350)
    expect(onChange.mock.calls).toEqual([["playwright"]])
  })

  it("shows what is being typed before it is committed", () => {
    mount(vi.fn())
    type("play")
    expect(box()).toHaveProperty("value", "play")
  })

  it("commits immediately on Enter rather than making the operator wait it out", () => {
    const onChange = vi.fn()
    mount(onChange)
    type("playwright")
    fireEvent.keyDown(box(), { key: "Enter" })
    expect(onChange).toHaveBeenCalledWith("playwright")
  })

  // The committed value lives in the URL, so the back button can change it
  // under the box; the field has to follow rather than hold the old text.
  it("follows the value it is given", () => {
    const { rerender } = mount(vi.fn(), "git")
    expect(box()).toHaveProperty("value", "git")
    rerender(<SearchBox value="" onChange={vi.fn()} label="搜索商店条目" placeholder="搜索" />)
    expect(box()).toHaveProperty("value", "")
  })
})
