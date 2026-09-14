import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"
import { PlaybackBar } from "./PlaybackBar"
import type { PlaybackActions, PlaybackModel } from "./usePlaybackControls"

vi.mock("react-i18next", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react-i18next")>()
  const t = (key: string, vars?: Record<string, unknown>) => (vars ? `${key} ${JSON.stringify(vars)}` : key)
  return { ...actual, useTranslation: () => ({ t, i18n: { language: "en-US" } }) }
})

afterEach(cleanup)

const BIG = "9007199254740993"

function model(overrides: Partial<PlaybackModel> = {}): PlaybackModel {
  return {
    live: false,
    playing: false,
    loading: false,
    current: "9007199254740990",
    floor: "9007199254740980",
    loadedSeq: BIG,
    headSeq: "9007199254740995",
    newer: "3",
    rate: 1,
    skipIdle: true,
    previousEvent: "9007199254740989",
    nextEvent: "9007199254740991",
    previousStep: "9007199254740985",
    nextStep: null,
    ...overrides,
  }
}

function actions(): PlaybackActions {
  return { togglePlay: vi.fn(), seek: vi.fn(), setRate: vi.fn(), setSkipIdle: vi.fn(), returnToLive: vi.fn() }
}

describe("PlaybackBar", () => {
  it("steps by exact sequence strings beyond JavaScript's safe integers", () => {
    const handlers = actions()
    render(<PlaybackBar model={model()} actions={handlers} />)
    fireEvent.click(screen.getByTestId("trajectory-next-event"))
    expect(handlers.seek).toHaveBeenLastCalledWith("9007199254740991")
    fireEvent.click(screen.getByTestId("trajectory-previous-step"))
    expect(handlers.seek).toHaveBeenLastCalledWith("9007199254740985")
    expect((screen.getByTestId("trajectory-next-step") as HTMLButtonElement).disabled).toBe(true)
    fireEvent.change(screen.getByTestId("trajectory-seek-input"), { target: { value: BIG } })
    fireEvent.submit(screen.getByTestId("trajectory-seek-input").closest("form")!)
    expect(handlers.seek).toHaveBeenLastCalledWith(BIG)
  })

  it("keeps the replay position separate from the live head and offers an explicit return", () => {
    const handlers = actions()
    render(<PlaybackBar model={model()} actions={handlers} />)
    expect(screen.getByTestId("trajectory-effective-seq").getAttribute("data-seq")).toBe("9007199254740990")
    expect(screen.getByTestId("trajectory-live-head").getAttribute("data-seq")).toBe("9007199254740995")
    expect(screen.getByTestId("trajectory-newer-count").textContent).toContain('"n":"3"')
    fireEvent.click(screen.getByTestId("trajectory-return-live"))
    expect(handlers.returnToLive).toHaveBeenCalledOnce()
    cleanup()
    render(<PlaybackBar model={model({ live: true, newer: "0" })} actions={handlers} />)
    expect(screen.queryByTestId("trajectory-return-live")).toBeNull()
    expect(screen.getByTestId("trajectory-mode").getAttribute("data-live")).toBe("true")
  })

  it("offers only viewing controls — nothing that acts on the watched session", () => {
    render(<PlaybackBar model={model()} actions={actions()} />)
    const names = screen
      .getAllByRole("button")
      .map((button) => button.getAttribute("aria-label") ?? button.textContent ?? "")
    expect(names.every((name) => name.startsWith("playback."))).toBe(true)
    expect(names.join(" ")).not.toMatch(/approve|answer|send|cancel|resume|retry|provision/i)
  })
})
