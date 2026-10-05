import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import type { BrowserSnapshot } from "../api/private-browser"
import { PrivateBrowserTab } from "./PrivateBrowserTab"

vi.mock("react-i18next", () => ({ useTranslation: () => ({ t: (key: string) => key }) }))

const BASE = "/api/assistant/browser-resources"
const RESOURCE = "a".repeat(64)
const TOKEN = "human-grant-ephemeral-fixture"
type Call = { path: string; method: string; body: Record<string, unknown>; headers: Headers }
let calls: Call[]
let resource: BrowserSnapshot | null
let intercept: ((call: Call) => Promise<Response> | Response | undefined) | undefined

function snapshot(): BrowserSnapshot {
  return { resource_id: RESOURCE, resource_type: "browser_profile",
    fence: { resource_id: RESOURCE, epoch: 1, owner_kind: "automation", owner_id: "workspace" },
    status: "active", admission: "open", expires_at: null, remote_available: true,
    can_takeover: true, can_giveback: false, fresh_observation_required: false }
}
function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } })
}
function serve(call: Call): Response {
  if (call.path === `${BASE}/current`) return json({ resource })
  if (call.path === `${BASE}/ensure`) { resource = snapshot(); return json(resource) }
  if (call.path.endsWith("/control")) {
    const row = resource!
    if (call.body.action !== "close") {
      row.fence = { resource_id: RESOURCE, epoch: row.fence.epoch + 1,
        owner_kind: call.body.action === "takeover" ? "human" : "automation",
        owner_id: call.body.action === "takeover" ? "owner" : "workspace" }
      row.status = "active"
      row.admission = "open"
    } else { row.status = "hold"; row.admission = "closed" }
    row.expires_at = new Date(Date.now() + 120_000).toISOString()
    row.can_takeover = row.fence.owner_kind === "automation" && row.admission === "open"
    row.can_giveback = row.fence.owner_kind === "human"
    return json({ state: "applied", command_id: "control", fence: row.fence,
      ...(call.body.action === "takeover" ? { human_token: TOKEN, expires_at: row.expires_at } : {}) })
  }
  if (call.path.endsWith("/operations")) {
    expect(call.body.human_token).toBe(TOKEN)
    expect(call.body.fence).toEqual(resource!.fence)
    return json({ state: "completed", fence: resource!.fence, operation_id: call.body.operation_id,
      result: call.body.kind === "capture" ? { png_base64: "iVBORw0KGgo=", observation: {
        eligible: true, fence: resource!.fence, width: 1024, height: 768, url: "https://private.test/owned" } } : { delivered: true } })
  }
  if (call.path.endsWith("/heartbeat")) return json({ fence: resource!.fence, expires_at: new Date(Date.now() + 120_000).toISOString() })
  throw new Error("Unexpected request: " + call.path)
}
function pendingResponse() {
  let resolve!: (response: Response) => void
  return { promise: new Promise<Response>((done) => { resolve = done }), resolve: (response: Response) => resolve(response) }
}
function button(key: string) { return screen.getByRole("button", { name: `privateBrowser.${key}` }) as HTMLButtonElement }
async function ready() {
  render(<PrivateBrowserTab />)
  await waitFor(() => expect(button("takeover").disabled).toBe(false))
}
async function takeControl() {
  fireEvent.click(button("takeover"))
  await screen.findByAltText("privateBrowser.frame")
  await waitFor(() => expect(button("capture").disabled).toBe(false))
}

beforeEach(() => {
  calls = []
  resource = snapshot()
  intercept = undefined
  useAuthStore.setState({ accessToken: "access", isAuthenticated: true, isLoading: false,
    user: { id: "owner", username: "owner", role: "user" } })
  useWorkspaceStore.setState({ currentId: "workspace" })
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, options?: RequestInit) => {
    const path = new URL(String(input), "http://local.test").pathname
    const call = { path, method: options?.method ?? "GET", body: options?.body ? JSON.parse(String(options.body)) : {},
      headers: new Headers(options?.headers) }
    calls.push(call)
    return intercept?.(call) ?? serve(call)
  }))
})
afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.unstubAllGlobals()
  localStorage.clear()
})

describe("private browser handover through the real HTTP adapter", () => {
  it("retains a preparation failure across automatic empty polls until an explicit check or retry", async () => {
    vi.useFakeTimers()
    resource = null
    let failPrepare = true
    intercept = (call) => call.path.endsWith("/ensure") && failPrepare
      ? json({ detail: { code: "BROWSER_STARTUP_PENDING" } }, 423) : undefined
    await act(async () => { render(<PrivateBrowserTab />) })
    await act(async () => { fireEvent.click(button("prepare")) })
    expect(screen.getByRole("alert")).toBeTruthy()
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000) })
    expect(calls.filter((call) => call.path.endsWith("/current"))).toHaveLength(3)
    expect(screen.getByRole("alert")).toBeTruthy()
    expect(screen.queryByRole("button", { name: "privateBrowser.prepare" })).toBeNull()
    expect(calls.filter((call) => call.path.endsWith("/ensure"))).toHaveLength(1)
    await act(async () => { fireEvent.click(button("check")) })
    expect(screen.queryByRole("alert")).toBeNull()
    failPrepare = false
    await act(async () => { fireEvent.click(button("prepare")) })
    expect(button("takeover").disabled).toBe(false)
    expect(calls.filter((call) => call.path.endsWith("/ensure"))).toHaveLength(2)
  })

  it("recovers a polling failure without restoring a grant that lost permission", async () => {
    vi.useFakeTimers()
    await act(async () => { render(<PrivateBrowserTab />) })
    await act(async () => { fireEvent.click(button("takeover")) })
    expect(screen.getByAltText("privateBrowser.frame")).toBeTruthy()
    intercept = (call) => call.path.endsWith("/current") ? json({ detail: "Forbidden" }, 403) : undefined
    await act(async () => { await vi.advanceTimersByTimeAsync(5_000) })
    expect(screen.getByRole("alert")).toBeTruthy()
    expect(screen.queryByAltText("privateBrowser.frame")).toBeNull()
    expect(button("capture").disabled).toBe(true)
    intercept = undefined
    await act(async () => { await vi.advanceTimersByTimeAsync(5_000) })
    expect(screen.queryByRole("alert")).toBeNull()
    expect(button("capture").disabled).toBe(true)
    expect(calls.filter((call) => call.path.endsWith("/operations"))).toHaveLength(1)
  })

  it("does not let an older explicit check dismiss a later preparation failure", async () => {
    vi.useFakeTimers()
    resource = null
    await act(async () => { render(<PrivateBrowserTab />) })
    const old = pendingResponse()
    intercept = (call) => call.path.endsWith("/current") ? old.promise
      : call.path.endsWith("/ensure") ? json({ detail: "Starting" }, 423) : undefined
    await act(async () => { fireEvent.click(button("check")) })
    await act(async () => { fireEvent.click(button("prepare")) })
    expect(screen.getByRole("alert")).toBeTruthy()
    await act(async () => { old.resolve(json({ resource: null })) })
    expect(screen.getByRole("alert")).toBeTruthy()
  })

  it("only reads on mount and requires explicit provisioning, then explicit takeover", async () => {
    resource = null
    render(<PrivateBrowserTab />)
    await screen.findByRole("button", { name: "privateBrowser.prepare" })
    expect(calls.map((call) => [call.method, call.path])).toEqual([["GET", `${BASE}/current`]])
    expect(button("capture").disabled).toBe(true)
    fireEvent.click(button("prepare"))
    await waitFor(() => expect(button("takeover").disabled).toBe(false))
    expect(calls.filter((call) => call.path.endsWith("/control"))).toHaveLength(0)
    await takeControl()
    expect(calls.some((call) => call.body.kind === "capture")).toBe(true)
    expect(calls.every((call) => call.path.startsWith(BASE))).toBe(true)
  })

  it("does not show usable control when profile readiness is absent", async () => {
    resource!.remote_available = false
    resource!.can_takeover = false
    render(<PrivateBrowserTab />)
    await screen.findByRole("button", { name: "privateBrowser.takeover" })
    expect(button("takeover").disabled).toBe(true)
    expect(button("capture").disabled).toBe(true)
    expect(screen.queryByAltText("privateBrowser.frame")).toBeNull()
    expect(calls).toHaveLength(1)
  })

  it.each(["draining", "lost-response"])("continues the same command after %s without claiming control", async (mode) => {
    let first = true
    intercept = (call) => {
      if (!call.path.endsWith("/control") || !first) return
      first = false
      return mode === "draining" ? json({ state: "draining", command_id: "pending" })
        : Promise.reject(new TypeError("Connection lost"))
    }
    await ready()
    fireEvent.click(button("takeover"))
    await waitFor(() => expect(button("continue").disabled).toBe(false))
    expect(button("capture").disabled).toBe(true)
    expect(calls.filter((call) => call.path.endsWith("/operations"))).toHaveLength(0)
    fireEvent.click(button("continue"))
    await screen.findByAltText("privateBrowser.frame")
    const requests = calls.filter((call) => call.path.endsWith("/control"))
    expect(requests).toHaveLength(2)
    expect(requests[1].body).toEqual(requests[0].body)
  })

  it("recovers the original pending control key after a viewer reload", async () => {
    resource!.status = "draining"
    resource!.admission = "closed"
    resource!.pending_control = { action: "takeover", expected_epoch: 1, idempotency_key: "original-device-key", command_id: "original-command" }
    intercept = (call) => {
      if (!call.path.endsWith("/control")) return
      resource!.pending_control = null
      return serve(call)
    }
    render(<PrivateBrowserTab />)
    const confirm = await screen.findByRole("button", { name: "privateBrowser.continue" })
    expect(button("capture").disabled).toBe(true)
    fireEvent.click(confirm)
    await screen.findByAltText("privateBrowser.frame")
    expect(calls.find((call) => call.path.endsWith("/control"))?.body).toEqual({ action: "takeover", expected_epoch: 1, idempotency_key: "original-device-key" })
  })

  it("refreshes a recovered expired grant immediately so it can be explicitly returned without a token", async () => {
    let attempts = 0
    intercept = (call) => {
      if (!call.path.endsWith("/control") || call.body.action !== "takeover") return
      attempts += 1
      resource!.fence = { resource_id: RESOURCE, epoch: 2, owner_kind: "human", owner_id: "owner" }
      resource!.status = "hold"
      resource!.admission = "closed"
      resource!.expires_at = new Date(Date.now() - 1_000).toISOString()
      resource!.can_takeover = false
      resource!.can_giveback = true
      return attempts === 1 ? Promise.reject(new TypeError("Takeover response lost"))
        : json({ state: "applied", command_id: "original-expired", fence: resource!.fence, human_grant_expired: true })
    }
    await ready()
    fireEvent.click(button("takeover"))
    await waitFor(() => expect(button("continue").disabled).toBe(false))
    fireEvent.click(button("continue"))
    await waitFor(() => expect(button("giveback").disabled).toBe(false))
    expect(calls.filter((call) => call.path.endsWith("/current"))).toHaveLength(2)
    expect(screen.queryByRole("alert")).toBeNull()
    expect(screen.queryByAltText("privateBrowser.frame")).toBeNull()
    expect(button("capture").disabled).toBe(true)
    const control = calls.filter((call) => call.path.endsWith("/control"))
    expect(control).toHaveLength(2)
    expect(control[1].body).toEqual(control[0].body)
    fireEvent.click(button("giveback"))
    await screen.findByText("privateBrowser.givenBack")
    expect(calls.filter((call) => call.path.endsWith("/control"))[2].body.expected_epoch).toBe(2)
    expect(calls.filter((call) => call.path.endsWith("/operations") || call.path.endsWith("/heartbeat"))).toHaveLength(0)
    expect(calls.some((call) => "human_token" in call.body)).toBe(false)
  })

  it("ignores a stale state read that finishes after the takeover receipt", async () => {
    vi.useFakeTimers()
    await act(async () => { render(<PrivateBrowserTab />) })
    const old = pendingResponse()
    let holdOne = true
    const stale = structuredClone(resource)
    intercept = (call) => {
      if (!call.path.endsWith("/current") || !holdOne) return
      holdOne = false
      return old.promise
    }
    await act(async () => { await vi.advanceTimersByTimeAsync(5_000) })
    await act(async () => { fireEvent.click(button("takeover")) })
    expect(screen.getByAltText("privateBrowser.frame")).toBeTruthy()
    await act(async () => { old.resolve(json({ resource: stale })) })
    expect(screen.getByAltText("privateBrowser.frame")).toBeTruthy()
    expect(button("capture").disabled).toBe(false)
  })

  it("keeps status reads working after a failed control invalidates an in-flight poll", async () => {
    vi.useFakeTimers()
    await act(async () => { render(<PrivateBrowserTab />) })
    const old = pendingResponse()
    const stale = structuredClone(resource)
    let holdOne = true
    intercept = (call) => {
      if (call.path.endsWith("/control")) return Promise.reject(new TypeError("Connection lost"))
      if (!call.path.endsWith("/current") || !holdOne) return
      holdOne = false
      return old.promise
    }
    await act(async () => { await vi.advanceTimersByTimeAsync(5_000) })
    expect(calls.filter((call) => call.path.endsWith("/current"))).toHaveLength(2)
    await act(async () => { fireEvent.click(button("takeover")) })
    expect(screen.getByRole("alert")).toBeTruthy()
    expect(button("continue").disabled).toBe(false)
    await act(async () => { old.resolve(json({ resource: stale })) })
    expect(screen.getByRole("alert")).toBeTruthy()

    resource = null
    await act(async () => { fireEvent.click(button("check")) })
    expect(calls.filter((call) => call.path.endsWith("/current"))).toHaveLength(3)
    expect(screen.queryByRole("alert")).toBeNull()
    expect(button("prepare").disabled).toBe(false)
    resource = snapshot()
    await act(async () => { await vi.advanceTimersByTimeAsync(5_000) })
    expect(calls.filter((call) => call.path.endsWith("/current"))).toHaveLength(4)
    expect(screen.queryByRole("button", { name: "privateBrowser.prepare" })).toBeNull()
    expect(button("continue").disabled).toBe(false)
    expect(calls.filter((call) => call.path.endsWith("/control"))).toHaveLength(1)
    expect(calls.filter((call) => call.path.endsWith("/operations"))).toHaveLength(0)
  })

  it("sends finite input with the current fence, scales image clicks, and recaptures after each input", async () => {
    await ready(); await takeControl()
    const image = screen.getByRole("button", { name: "privateBrowser.clickFrame" })
    vi.spyOn(image, "getBoundingClientRect").mockReturnValue({ left: 10, top: 20, width: 512, height: 384 } as DOMRect)
    fireEvent.click(image, { clientX: 266, clientY: 212 })
    await waitFor(() => expect(button("capture").disabled).toBe(false))
    expect(calls.find((call) => call.body.kind === "mouse")?.body.args).toEqual({ x: 512, y: 384, button: "left" })
    fireEvent.change(screen.getByLabelText("privateBrowser.text"), { target: { value: "输入文本" } })
    fireEvent.click(button("send"))
    await waitFor(() => expect(button("capture").disabled).toBe(false))
    expect(calls.find((call) => call.body.kind === "text")?.body.args).toEqual({ text: "输入文本" })
    fireEvent.click(button("press"))
    await waitFor(() => expect(button("capture").disabled).toBe(false))
    fireEvent.click(button("scrollDown"))
    await waitFor(() => expect(button("capture").disabled).toBe(false))
    fireEvent.change(screen.getByLabelText("privateBrowser.address"), { target: { value: "https://private.test/next" } })
    fireEvent.click(button("go"))
    await waitFor(() => expect(button("capture").disabled).toBe(false))
    const operations = calls.filter((call) => call.path.endsWith("/operations"))
    expect(operations.map((call) => call.body.kind)).toEqual(["capture", "mouse", "capture", "text", "capture", "key", "capture", "wheel", "capture", "navigate", "capture"])
    expect(new Set(operations.map((call) => call.body.operation_id)).size).toBe(operations.length)
    expect(calls.every((call) => call.headers.get("X-Workspace-Id") === "workspace")).toBe(true)
    expect(JSON.stringify(localStorage)).not.toContain(TOKEN)
    expect(location.href).not.toContain(TOKEN)
    expect(document.body.textContent).not.toContain(TOKEN)
  })

  it("clears the image and token after response revocation, and never retries the input", async () => {
    await ready(); await takeControl()
    intercept = (call) => call.body.kind === "key" ? json({ detail: { code: "BROWSER_TOKEN_EXPIRED", message: "revoked" } }, 403) : undefined
    fireEvent.click(button("press"))
    await screen.findByRole("alert")
    expect(screen.queryByAltText("privateBrowser.frame")).toBeNull()
    expect(button("press").disabled).toBe(true)
    fireEvent.click(button("check"))
    await waitFor(() => expect(screen.queryByRole("alert")).toBeNull())
    expect(button("capture").disabled).toBe(true)
    expect(calls.filter((call) => call.body.kind === "key")).toHaveLength(1)
  })

  it("allows explicit close while an input is in flight and discards that late response", async () => {
    await ready(); await takeControl()
    const late = pendingResponse()
    intercept = (call) => call.body.kind === "key" ? late.promise : undefined
    fireEvent.click(button("press"))
    await waitFor(() => expect(calls.some((call) => call.body.kind === "key")).toBe(true))
    expect(button("stop").disabled).toBe(false)
    fireEvent.click(button("stop"))
    await waitFor(() => expect(calls.some((call) => call.body.action === "close")).toBe(true))
    await act(async () => {
      late.resolve(json({ state: "completed", operation_id: "old-input", fence: resource!.fence, result: { delivered: true } }))
    })
    expect(screen.queryByAltText("privateBrowser.frame")).toBeNull()
    expect(button("capture").disabled).toBe(true)
    expect(calls.filter((call) => call.body.kind === "capture")).toHaveLength(1)
  })

  it.each(["user", "workspace", "unmount"])("drops a late takeover on %s change without using its grant", async (change) => {
    const late = pendingResponse()
    intercept = (call) => call.path.endsWith("/control") ? late.promise : undefined
    const view = render(<PrivateBrowserTab />)
    await waitFor(() => expect(button("takeover").disabled).toBe(false))
    fireEvent.click(button("takeover"))
    await waitFor(() => expect(calls.some((call) => call.path.endsWith("/control"))).toBe(true))
    await act(async () => {
      if (change === "user") useAuthStore.setState({ user: { id: "peer", username: "peer", role: "user" } })
      else if (change === "workspace") useWorkspaceStore.setState({ currentId: "other" })
      else view.unmount()
      late.resolve(json({ state: "applied", command_id: "late", fence: { ...snapshot().fence, epoch: 2, owner_kind: "human", owner_id: "owner" },
        human_token: TOKEN, expires_at: new Date(Date.now() + 120_000).toISOString() }))
    })
    expect(calls.filter((call) => call.path.endsWith("/operations"))).toHaveLength(0)
    expect(screen.queryByAltText("privateBrowser.frame")).toBeNull()
  })

  it("returns control without claiming that paused tasks resumed", async () => {
    await ready(); await takeControl()
    fireEvent.click(button("giveback"))
    await screen.findByText("privateBrowser.givenBack")
    expect(screen.queryByAltText("privateBrowser.frame")).toBeNull()
    expect(button("capture").disabled).toBe(true)
    expect(calls.filter((call) => call.path.includes("/tasks/") || call.path.includes("/turns"))).toHaveLength(0)
  })

  it("renews only the mounted grant and stops input on a failed heartbeat", async () => {
    vi.useFakeTimers()
    await act(async () => { render(<PrivateBrowserTab />) })
    await act(async () => { fireEvent.click(button("takeover")) })
    expect(screen.getByAltText("privateBrowser.frame")).toBeTruthy()
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000) })
    expect(calls.filter((call) => call.path.endsWith("/heartbeat"))).toHaveLength(1)
    intercept = (call) => call.path.endsWith("/heartbeat") ? json({ detail: "expired" }, 403) : undefined
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000) })
    expect(screen.queryByAltText("privateBrowser.frame")).toBeNull()
    expect(button("capture").disabled).toBe(true)
    cleanup()
    const before = calls.length
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000) })
    expect(calls).toHaveLength(before)
  })
})
