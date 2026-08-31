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
  }
  const createSession = vi.fn((id: string, options: Record<string, unknown>) => {
    void id
    void options
    return session
  })

  beforeEach(() => {
    handlers.clear()
    vi.stubGlobal("ResizeObserver", ResizeObserverStub)
    vi.mocked(http.get).mockResolvedValue({
      ticket: "ticket",
      desktopId: "ecd-test",
      regionId: "cn-hangzhou",
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
    expect(options.uiConfig).toMatchObject({ defaultResolution: "B" })
    expect(options.uiConfig).not.toHaveProperty("resolutionType")
    expect(options.desktopInfo.connConfig).toMatchObject({
      useCustomIme: true,
      disableIME: false,
      resolutionAdaptive: true,
      enableAutoSwitchMouseMode: true,
      mediaSuspendedTipFlag: 27,
    })

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
