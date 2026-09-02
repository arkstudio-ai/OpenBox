import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { http } from "@/shared/api/http"
import { DesktopTab } from "./DesktopTab"

vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}))

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
    // The tab asks /api/desktop/status before the ticket; answer by path so
    // the shared-desktop "running" state lets the connect proceed.
    vi.mocked(http.get).mockImplementation(async (path: string) => {
      if (path.startsWith("/api/desktop/status")) return { state: "running", mode: "shared" }
      return { ticket: "ticket", desktopId: "ecd-test", regionId: "cn-hangzhou" }
    })
    Object.assign(window, { Wuying: { WebSDK: { createSession } } })
  })

  afterEach(() => {
    cleanup()
    vi.clearAllMocks()
    vi.unstubAllGlobals()
    delete (window as Window & { Wuying?: unknown }).Wuying
  })

  it("uses an untransformed iframe and the official mouse and IME settings", async () => {
    render(<DesktopTab />)

    await waitFor(() => expect(createSession).toHaveBeenCalledOnce())
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

    fireEvent.click(screen.getByRole("checkbox", { name: "desktop.allowControl" }))
    expect(session.setInputEnabled).toHaveBeenLastCalledWith(true)
    expect(session.enableKeyBoard).toHaveBeenLastCalledWith(true)
    expect(session.setTouchEnabled).toHaveBeenLastCalledWith(true)
    expect(session.setMouseMode).toHaveBeenLastCalledWith("Client")
    expect(document.activeElement).toBe(frame)
    expect(screen.getByText("desktop.imeHint")).toBeTruthy()
  })
})
