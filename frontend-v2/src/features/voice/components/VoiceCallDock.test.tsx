import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { initialCall } from "../lib/reducer"
import type { CallEnd, CallState } from "../lib/types"
import { useVoiceStore } from "../store"
import { VoiceCallDock } from "./VoiceCallDock"

vi.mock("react-i18next", async (importOriginal) => ({
  ...(await importOriginal<typeof import("react-i18next")>()),
  useTranslation: () => ({
    t: (key: string, options?: Record<string, unknown>) =>
      options ? `${key}:${JSON.stringify(options)}` : key,
  }),
}))

const pristine = useVoiceStore.getState()

function setCall(call: Partial<CallState>, extra: Partial<ReturnType<typeof useVoiceStore.getState>> = {}) {
  useVoiceStore.setState({ call: { ...initialCall, ...call }, ...extra })
}

function connected(
  call: Partial<CallState> = {},
  extra: Partial<ReturnType<typeof useVoiceStore.getState>> = {},
) {
  setCall(
    {
      status: "connected",
      phase: "listening",
      callId: "call-1",
      startedAt: Date.now() - 74_000,
      maxSeconds: 1800,
      ...call,
    },
    extra,
  )
}

function ended(end: Partial<CallEnd>) {
  setCall({
    status: "ended",
    ended: {
      reason: "hangup",
      durationSeconds: 74,
      pendingTurns: 0,
      cost: null,
      errorKey: null,
      errorMessage: null,
      ...end,
    },
  })
}

beforeEach(() => {
  useVoiceStore.setState({ ...pristine, expanded: true })
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  useVoiceStore.setState(pristine, true)
})

describe("VoiceCallDock", () => {
  it("renders nothing between calls", () => {
    const { container } = render(<VoiceCallDock />)
    expect(container.firstChild).toBeNull()
  })

  it("shows the phase, the call clock and the controls while connected", async () => {
    connected({
      cost: { total_yuan: "0.003500", settled_rounds: 2, costs_yuan: { input_audio: "0.001000" } },
    })
    render(<VoiceCallDock />)
    const window = screen.getByRole("region", { name: "title" })
    expect(within(window).getByRole("status").textContent).toContain("state.listening")
    expect(await within(window).findByText("01:14")).toBeTruthy()
    expect(within(window).getByRole("button", { name: /controls\.mute/ })).toBeTruthy()
    const cost = within(window).getByText("¥0.0035")
    expect(cost.getAttribute("title")).toContain("cost.items.inputAudio ¥0.001000")
    expect(cost.getAttribute("title")).toContain('cost.settled:{"count":2}')
    // The "just talk" hint is for the first seconds of a call only.
    expect(within(window).queryByText("hint.start")).toBeNull()
  })

  it("greets a fresh call with the hint", () => {
    connected({ phase: "greeting", startedAt: Date.now() })
    render(<VoiceCallDock />)
    expect(screen.getByRole("status").textContent).toBe("state.greeting")
    expect(screen.getByText("hint.start")).toBeTruthy()
  })

  it("notes a turn still being worked on beside the phase, and says when it is late", () => {
    connected({ phase: "speaking", working: true })
    const { rerender } = render(<VoiceCallDock />)
    expect(screen.getByRole("status").textContent).toBe("state.speakingstate.working")
    connected({ phase: "working", working: true, late: true })
    rerender(<VoiceCallDock />)
    expect(screen.getByRole("status").textContent).toBe("state.late")
  })

  it("says how many minutes are left near the end", async () => {
    connected({ maxSeconds: 200 })
    render(<VoiceCallDock />)
    expect(await screen.findByText('duration.remaining:{"minutes":3}')).toBeTruthy()
  })

  it("collapses to the pill and expands back; the pill keeps its own hang-up", () => {
    const hangUp = vi.fn()
    connected({}, { hangUp })
    render(<VoiceCallDock />)
    fireEvent.click(screen.getByRole("button", { name: "controls.collapse" }))
    expect(useVoiceStore.getState().expanded).toBe(false)
    expect(screen.queryByRole("button", { name: "controls.collapse" })).toBeNull()
    expect(screen.getByRole("status").textContent).toBe("state.listening")
    fireEvent.click(screen.getByRole("button", { name: "controls.hangUp" }))
    expect(hangUp).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getByRole("button", { name: "controls.expand" }))
    expect(useVoiceStore.getState().expanded).toBe(true)
  })

  it("hangs up from the card in one click", () => {
    const hangUp = vi.fn()
    connected({}, { hangUp })
    render(<VoiceCallDock />)
    fireEvent.click(screen.getByRole("button", { name: /controls\.hangUp/ }))
    expect(hangUp).toHaveBeenCalledTimes(1)
  })

  it("only offers to cancel while dialling, and nothing while hanging up", () => {
    setCall({ status: "requesting_mic" })
    const { rerender } = render(<VoiceCallDock />)
    expect(screen.getByRole("status").textContent).toBe("state.requestingMic")
    expect(screen.queryByRole("button", { name: /controls\.mute/ })).toBeNull()
    // Dialling: the grey button only cancels.
    expect(screen.getByRole("button", { name: /controls\.cancel/ }).className).toContain("bg-n200")
    // Hanging up: the clock stops where the user hung up, and the cost stays in view.
    connected({ status: "ending", stoppedAt: Date.now() - 4_000, cost: { total_yuan: "0.0042" } })
    rerender(<VoiceCallDock />)
    expect(screen.getByRole("status").textContent).toBe("state.ending")
    expect(screen.getByText("01:10")).toBeTruthy()
    expect(screen.getByText("¥0.0042")).toBeTruthy()
    expect(screen.queryByRole("button", { name: /controls\.mute/ })).toBeNull()
    expect((screen.getByRole("button", { name: /controls\.hangUp/ }) as HTMLButtonElement).disabled).toBe(
      true,
    )
  })

  it("ends with what happened, how long and how much, and what is still being worked on", () => {
    ended({ reason: "network", pendingTurns: 2, cost: { total_yuan: "0.003500" } })
    render(<VoiceCallDock />)
    const panel = screen.getByRole("region", { name: "title" })
    expect(within(panel).getByText("ended.title")).toBeTruthy()
    expect(within(panel).getByText("ended.network")).toBeTruthy()
    expect(
      within(panel).getByText('ended.duration:{"duration":"01:14"} · ended.cost:{"yuan":"0.0035"}'),
    ).toBeTruthy()
    expect(within(panel).getByText('ended.pendingHint:{"count":2}')).toBeTruthy()
    expect(within(panel).getByRole("button", { name: "actions.redial" })).toBeTruthy()
  })

  it("explains an error with the client's copy or the server's own words", () => {
    ended({ reason: "error", errorKey: "connectFailed", durationSeconds: 0 })
    const { rerender } = render(<VoiceCallDock />)
    expect(screen.getByText("ended.error")).toBeTruthy()
    expect(screen.getByText("errors.connectFailed")).toBeTruthy()
    expect(screen.queryByText(/ended\.duration/)).toBeNull()
    ended({ reason: "error", errorMessage: "语音服务暂时不可用" })
    rerender(<VoiceCallDock />)
    expect(screen.getByText("语音服务暂时不可用")).toBeTruthy()
  })

  it.each([
    ["hangup", true],
    ["limit", true],
    ["error", true],
    ["network", true],
    ["quota", false],
    ["concurrent", false],
    ["mic_denied", false],
  ] as const)("offers a redial after %s: %s", (reason, offered) => {
    ended({ reason })
    render(<VoiceCallDock />)
    expect(screen.queryByRole("button", { name: "actions.redial" }) !== null).toBe(offered)
  })

  it("redials and closes from the panel", () => {
    const start = vi.fn()
    ended({ reason: "hangup" })
    useVoiceStore.setState({ start })
    render(<VoiceCallDock />)
    fireEvent.click(screen.getByRole("button", { name: "actions.redial" }))
    expect(start).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getByRole("button", { name: "actions.close" }))
    expect(useVoiceStore.getState().call.status).toBe("idle")
    expect(screen.queryByRole("region")).toBeNull()
  })

  it("closes a normal ending by itself after 8 s and keeps a failure up", () => {
    vi.useFakeTimers()
    ended({ reason: "hangup" })
    const { rerender } = render(<VoiceCallDock />)
    act(() => vi.advanceTimersByTime(7_999))
    expect(screen.getByRole("region")).toBeTruthy()
    act(() => vi.advanceTimersByTime(1))
    expect(useVoiceStore.getState().call.status).toBe("idle")
    ended({ reason: "network" })
    rerender(<VoiceCallDock />)
    act(() => vi.advanceTimersByTime(30_000))
    expect(useVoiceStore.getState().call.status).toBe("ended")
  })
})
