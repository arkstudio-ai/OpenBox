import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { ApiError, http } from "@/shared/api/http"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import { DesktopTab } from "./DesktopTab"

vi.mock("react-i18next", () => {
  const t = (key: string) => key
  return { useTranslation: () => ({ t }) }
})

vi.mock("@/shared/api/http", async (original) => ({
  ...await original<typeof import("@/shared/api/http")>(),
  http: { get: vi.fn() },
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
    useAuthStore.setState({ user: null })
    useWorkspaceStore.setState({ currentId: null })
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

  it("explains managed native access without reconnecting, then resets for a new account, workspace or viewer", async () => {
    vi.useFakeTimers()
    const ticket = vi.fn(async () => { throw new ApiError(423, "RESOURCE_CONTROL_HELD", "Locked") })
    vi.mocked(http.get).mockImplementation(async (path) => path.endsWith("/status")
      ? { state: "running", mode: "shared" } : ticket())
    let viewer: ReturnType<typeof render>
    await act(async () => { viewer = render(<DesktopTab />) })
    expect(screen.getByText("desktop.resourceControlHint")).toBeTruthy()
    expect(screen.queryByRole("button", { name: "desktop.reconnect" })).toBeNull()
    expect(screen.queryByRole("checkbox", { name: "desktop.allowControl" })).toBeNull()
    expect(createSession).not.toHaveBeenCalled()
    await act(async () => { await vi.advanceTimersByTimeAsync(120_000) })
    expect(ticket).toHaveBeenCalledOnce()

    await act(async () => { useWorkspaceStore.setState({ currentId: "another-workspace" }) })
    expect(ticket).toHaveBeenCalledTimes(2)
    viewer!.unmount()
    await act(async () => { render(<DesktopTab />) })
    expect(ticket).toHaveBeenCalledTimes(3)
    await act(async () => {
      useAuthStore.setState({ user: { id: "another-user", username: "another-user", role: "user" } })
    })
    expect(ticket).toHaveBeenCalledTimes(4)
    expect(screen.getByText("desktop.resourceControlHint")).toBeTruthy()
  })

  it("keeps explicit reconnect available for an ordinary temporary ticket error", async () => {
    const ticket = vi.fn()
      .mockRejectedValueOnce(new ApiError(503, "HTTP_503", "Unavailable"))
      .mockResolvedValue({ ticket: "ticket", desktopId: "ecd-test", regionId: "cn-hangzhou" })
    vi.mocked(http.get).mockImplementation(async (path) => path.endsWith("/status")
      ? { state: "running", mode: "shared" } : ticket())
    render(<DesktopTab />)
    fireEvent.click(await screen.findByRole("button", { name: "desktop.reconnect" }))
    await waitFor(() => expect(createSession).toHaveBeenCalledOnce())
    expect(ticket).toHaveBeenCalledTimes(2)
    expect(await screen.findByRole("checkbox", { name: "desktop.allowControl" })).toBeTruthy()
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
    // No floating "desktop inside the desktop": the stream's video may not enter picture-in-picture.
    expect(frame?.getAttribute("allow")).toContain("picture-in-picture 'none'")
    expect(frame?.getAttribute("allow")).toContain("fullscreen")

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
