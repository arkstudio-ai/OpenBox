import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { http } from "@/shared/api/http"
import { DesktopTab } from "./DesktopTab"

vi.mock("react-i18next", () => {
  const t = (key: string) => key
  return { useTranslation: () => ({ t }) }
})

vi.mock("@/shared/api/http", () => ({
  http: { get: vi.fn() },
  ApiError: class ApiError extends Error {},
}))

class ResizeObserverStub {
  observe() {}
  disconnect() {}
  unobserve() {}
}

describe("DesktopTab", () => {
  const handlers = new Map<string, () => void>()
  const session = {
    start: vi.fn(() => handlers.get("onConnected")?.()),
    stop: vi.fn(),
    addHandle: vi.fn((event: string, callback: () => void) => handlers.set(event, callback)),
    enableInput: vi.fn(),
    enableKeyBoard: vi.fn(),
    setInputEnabled: vi.fn(),
    setTouchEnabled: vi.fn(),
    setMouseMode: vi.fn(),
    setClipboardEnabled: vi.fn(),
    setResolution: vi.fn(),
  }
  const createSession = vi.fn((id: string, options: Record<string, unknown>) => {
    void id
    void options
    return session
  })

  beforeEach(() => {
    handlers.clear()
    vi.stubGlobal("ResizeObserver", ResizeObserverStub)
    // Answer per endpoint. `DesktopTab` checks /status before asking for a
    // ticket, and a status whose `state` is neither "running" nor
    // "not_provisioned" sends it into the provisioning poll — so a single
    // catch-all response would park the component on the loading screen and
    // never reach createSession.
    vi.mocked(http.get).mockImplementation(async (url: string) =>
      url.startsWith("/api/desktop/status")
        ? { state: "running", mode: "shared" }
        : { ticket: "ticket", desktopId: "ecd-test", regionId: "cn-hangzhou" },
    )
    Object.assign(window, { Wuying: { WebSDK: { createSession } } })
  })

  afterEach(() => {
    vi.useRealTimers()
    cleanup()
    vi.clearAllMocks()
    vi.unstubAllGlobals()
    delete (window as Window & { Wuying?: unknown }).Wuying
  })

  it("shows the paid-plan requirement without a ticket or cloud SDK session for free users", async () => {
    vi.mocked(http.get).mockResolvedValue({
      state: "subscription_required",
      entitled: false,
      retained: true,
      mode: "per_user",
    })
    render(<DesktopTab />)
    await screen.findByText("activation.subscriptionRequired")
    expect(createSession).not.toHaveBeenCalled()
    expect(vi.mocked(http.get).mock.calls.every(([path]) => path === "/api/desktop/status")).toBe(true)
  })

  it("stops an existing cloud session when its paid subscription expires", async () => {
    render(<DesktopTab />)
    await waitFor(() => expect(createSession).toHaveBeenCalledOnce())
    vi.useFakeTimers()
    // Re-render is unnecessary: the existing connection's status poll checks
    // server entitlement, including when the SDK session is already cached.
    vi.mocked(http.get).mockResolvedValue({
      state: "subscription_required",
      entitled: false,
      mode: "per_user",
    })
    // The interval was created with real timers; allow one deterministic tick
    // by unmounting/remounting under fake timers first.
    cleanup()
    vi.mocked(http.get).mockImplementation(async (path) =>
      path.endsWith("/status")
        ? { state: "running", entitled: true, mode: "per_user" }
        : { ticket: "ticket", desktopId: "ecd-test", regionId: "cn-hangzhou" },
    )
    await act(async () => {
      render(<DesktopTab />)
    })
    session.stop.mockClear()
    vi.mocked(http.get).mockResolvedValue({
      state: "subscription_required",
      entitled: false,
      mode: "per_user",
    })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000)
    })
    expect(session.stop).toHaveBeenCalledOnce()
    expect(screen.getByText("activation.subscriptionRequired")).toBeTruthy()
  })

  it("uses an untransformed iframe and the official mouse and IME settings", async () => {
    render(<DesktopTab />)

    await waitFor(() => expect(createSession).toHaveBeenCalledOnce())
    // SDK creation and React's connected UI are different readiness points.
    // Wait for the visible control before inspecting or interacting with it.
    const control = await screen.findByRole("checkbox", { name: "desktop.allowControl" })
    const stage = screen.getByTestId("desktop-stage")
    const frame = stage.querySelector("iframe")
    expect(frame).not.toBeNull()
    expect(frame?.parentElement).toBe(stage)
    expect(frame?.style.transform).toBe("")
    expect(frame?.style.position).toBe("absolute")

    const options = createSession.mock.calls[0][1] as {
      uiConfig: Record<string, unknown>
      desktopInfo: { connConfig: Record<string, unknown> }
    }
    expect(options.uiConfig).toMatchObject({ defaultResolution: "A" })
    expect(options.uiConfig).not.toHaveProperty("resolutionType")
    expect(options.desktopInfo.connConfig).toMatchObject({
      useCustomIme: true,
      disableIME: false,
      resolutionAdaptive: false,
      enableAutoSwitchMouseMode: true,
      mediaSuspendedTipFlag: 27,
    })

    // The SDK's first connect asks the desktop to match the pane ("A" per the
    // Web SDK docs); onConnected must pin the fixed 1080p mode straight back.
    expect(session.setResolution).toHaveBeenCalledWith(1920, 1080, 0)

    // Connected read-only state uses the preferred API once, without issuing
    // the duplicate legacy command that used to race it.
    expect(session.setInputEnabled).toHaveBeenLastCalledWith(false)
    expect(session.enableInput).not.toHaveBeenCalled()
    expect(session.enableKeyBoard).toHaveBeenLastCalledWith(false)

    fireEvent.click(control)
    expect(session.setInputEnabled).toHaveBeenLastCalledWith(true)
    expect(session.enableKeyBoard).toHaveBeenLastCalledWith(true)
    expect(session.setTouchEnabled).toHaveBeenLastCalledWith(true)
    expect(session.setMouseMode).toHaveBeenLastCalledWith("Client")
    expect(document.activeElement).toBe(frame)
    expect(screen.getByText("desktop.imeHint")).toBeTruthy()
  })
})
