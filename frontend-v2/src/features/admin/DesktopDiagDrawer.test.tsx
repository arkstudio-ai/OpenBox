import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { http } from "@/shared/api/http"
import { DesktopDiagDrawer } from "./DesktopDiagDrawer"

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string, options?: Record<string, unknown>) =>
      options && typeof options.defaultValue === "string" ? options.defaultValue : key,
  }),
}))
vi.mock("@/shared/api/http", () => ({ http: { get: vi.fn(), post: vi.fn() } }))

const get = vi.mocked(http.get)
const post = vi.mocked(http.post)

const events = {
  items: [
    {
      id: "dev_2", ts: "2026-09-07T10:00:00Z", kind: "browser.ensure", status: "fail",
      duration_ms: 15340, summary: "ChromeUnavailable: Chrome did not open its debug port",
      diag_id: "dev_1", session_id: "sess-1", tool_call_id: "call-1",
    },
    {
      id: "dev_1", ts: "2026-09-07T10:00:00Z", kind: "browser.diag", status: "ok",
      duration_ms: null, summary: "ChromeUnavailable | chrome=down", diag_id: null,
    },
  ],
}
const diagList = { items: [{ id: "dev_1", ts: "2026-09-07T10:00:00Z", reason: "ChromeUnavailable", collected: true }] }
const report = {
  diag_version: "t", collected_at: "2026-09-07T10:00:00Z", elapsed_ms: 1200, via: "action_server",
  errors: [{ section: "x", error: "obx-x: not found" }],
  summary: { lights: { chrome: "down", relay: "down", x: "unknown", unit: "ok", runtime: "ok" }, findings: ["no OpenBox Chrome process and :9333 not answering"] },
  chrome: { log: { lines: ["DevTools listening on ws://…", "Session terminated"] } },
  relay: { log: { missing: true } },
  logs: { journal: { lines: ["execute_trace {\"kind\":\"browser_probe\"}"] } },
}
const diagRecord = { id: "dev_1", ts: "2026-09-07T10:00:00Z", kind: "browser.diag", status: "ok", summary: "", reason: "ChromeUnavailable", report }

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const onClose = vi.fn()
  render(
    <QueryClientProvider client={client}>
      <DesktopDiagDrawer desktopId="ecd-1" onClose={onClose} />
    </QueryClientProvider>,
  )
  return { onClose }
}

beforeEach(() => {
  get.mockImplementation((url: string) => {
    if (url.includes("/events")) return Promise.resolve(events)
    if (url.includes("/diag/recent")) return Promise.resolve(diagList)
    if (url.includes("/diag/dev_1")) return Promise.resolve(diagRecord)
    return Promise.reject(new Error(`unexpected GET ${url}`))
  })
  post.mockReset()
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe("DesktopDiagDrawer", () => {
  it("shows the latest snapshot's lights, findings, log tails and the timeline", async () => {
    mount()
    await screen.findByText("no OpenBox Chrome process and :9333 not answering")
    expect(get).toHaveBeenCalledWith("/api/admin/fleet/desktops/ecd-1/events?limit=200")
    expect(get).toHaveBeenCalledWith("/api/admin/fleet/diag/recent?desktop_id=ecd-1&limit=20")
    const lights = screen.getByTestId("diag-lights").textContent
    expect(lights).toContain("diag.lights.chrome · diag.light.down")
    expect(lights).toContain("diag.lights.unit · diag.light.ok")
    expect(screen.getByText("diag.sections")).toBeTruthy()
    expect(screen.getByText("Session terminated", { exact: false })).toBeTruthy()
    // Timeline rows use translated kind names and cite their snapshot.
    // (the t mock returns the defaultValue, i.e. the raw kind)
    expect(screen.getByText("browser.ensure")).toBeTruthy()
    expect(screen.getByText("diag.cites")).toBeTruthy()
    // A row unfolds its correlation ids.
    fireEvent.click(screen.getByText("ChromeUnavailable: Chrome did not open its debug port"))
    expect(screen.getByText("sess-1")).toBeTruthy()
    expect(screen.getByText("call-1")).toBeTruthy()
  })

  it("takes a fresh snapshot and shows it in place of the stored one", async () => {
    post.mockResolvedValue({
      ...report,
      id: "dev_9",
      via: "cloud_assistant",
      fallback_errors: ["channel: ChannelNotReady: desktop channel is down"],
      summary: { lights: { chrome: "ok", relay: "ok", x: "ok", unit: "ok", runtime: "ok" }, findings: [] },
    })
    mount()
    await screen.findByText("no OpenBox Chrome process and :9333 not answering")
    fireEvent.click(screen.getByText("diag.collect"))
    await waitFor(() => expect(post).toHaveBeenCalledWith("/api/admin/fleet/desktops/ecd-1/diag", { via: "auto", lines: 60 }))
    await screen.findByText("diag.noFindings")
    expect(screen.getByText("diag.collectedVia")).toBeTruthy()
    expect(screen.getByText("diag.fallback")).toBeTruthy()
    expect(screen.getByTestId("diag-lights").textContent).toContain("diag.lights.chrome · diag.light.ok")
  })

  it("says so when there is no snapshot yet", async () => {
    get.mockImplementation((url: string) => {
      if (url.includes("/events")) return Promise.resolve({ items: [] })
      if (url.includes("/diag/recent")) return Promise.resolve({ items: [] })
      return Promise.reject(new Error(`unexpected GET ${url}`))
    })
    mount()
    await screen.findByText("diag.noSnapshot")
    expect(screen.getByText("diag.timelineEmpty")).toBeTruthy()
  })

  it("closes on Escape", async () => {
    const { onClose } = mount()
    await screen.findByText("diag.timeline")
    fireEvent.keyDown(window, { key: "Escape" })
    expect(onClose).toHaveBeenCalled()
  })
})
