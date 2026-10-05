import { Suspense } from "react"
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { createMemoryRouter, RouterProvider } from "react-router"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

vi.hoisted(() => {
  Object.defineProperty(globalThis, "matchMedia", { configurable: true, value: (media: string) => ({ media, matches: false,
    addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} }) })
  Object.defineProperty(globalThis, "ResizeObserver", { configurable: true, value: class { observe() {} unobserve() {} disconnect() {} } })
})

import "@/shared/i18n"
import WorkspaceLayout from "./WorkspaceLayout"
import { usePanelStore } from "@/features/workbench"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import { paths, routePatterns } from "@/shared/router/paths"
import { wsClient } from "@/shared/ws/client"

let calls: string[]
let sockets: string[]
let session: Record<string, unknown>
let responseGate: Promise<Response> | null
let refused: boolean

class Socket {
  static readonly OPEN = 1
  readyState = 0
  onopen = null
  onmessage = null
  onclose: (() => void) | null = null
  onerror = null
  constructor(url: string) { sockets.push(url) }
  close() { this.readyState = 3; this.onclose?.() }
  send() {}
}
function json(value: unknown, status = 200) { return new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } }) }
function response(path: string) {
  if (path === "/api/workspaces") return { items: [{ id: "workspace", name: "Workspace", owner_user_id: "owner", kind: "personal", role: "owner" }], default_workspace_id: "workspace" }
  if (path === "/api/agent/session/private-session") return session
  if (path === "/api/agent/session" || path === "/api/agent/project" || path === "/api/cron/jobs") return []
  if (path === "/api/assistant/browser-resources/current") return { resource: null }
  if (path === "/api/desktop/status") return { state: "subscription_required", entitled: false }
  if (path === "/api/assistant") return { state: "ready", session: { id: "main", kind: "assistant" }, tasks: [], answers: [], unread_count: 0 }
  if (path === "/api/auth/ticket") return { ticket: "test-ticket" }
  return {}
}
function mount(entry: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const router = createMemoryRouter([{ path: paths.app, element: <WorkspaceLayout />, children: [
    { path: routePatterns.chat, element: <p>Conversation</p> }, { path: "assistant", element: <p>Assistant</p> },
  ] }], { initialEntries: [entry] })
  render(<QueryClientProvider client={client}><Suspense fallback={null}><RouterProvider router={router} /></Suspense></QueryClientProvider>)
  return client
}
beforeEach(() => {
  calls = []; sockets = []; responseGate = null; refused = false
  session = { id: "private-session", user_id: "owner", workspace_id: "workspace", kind: "normal", agent: "build",
    visibility: "private", memory_policy: "assistant_isolated", assistant_managed: true, status: "idle", token_usage: {} }
  useAuthStore.setState({ accessToken: "access", user: { id: "owner", username: "owner", role: "user" }, isAuthenticated: true, isLoading: false })
  useWorkspaceStore.setState({ currentId: "workspace" })
  usePanelStore.setState({ open: true, tabs: [{ id: "desktop", kind: "desktop" }], activeTabId: "desktop" })
  vi.stubGlobal("WebSocket", Socket)
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = new URL(String(input), "http://local.test").pathname
    calls.push((init?.method ?? "GET") + " " + path)
    if (path === "/api/agent/session/private-session") {
      if (responseGate) return (await responseGate).clone()
      if (refused) return json({ detail: "unavailable" }, 403)
    }
    return json(response(path))
  }))
})
afterEach(() => {
  cleanup(); wsClient.disconnect(); vi.unstubAllGlobals()
  usePanelStore.setState({ open: false, tabs: [], activeTabId: null })
  useWorkspaceStore.getState().clear()
})

describe("private browser assembly in the existing workspace panel", () => {
  it("waits for the real Session query and never mounts shared UI before or after private resolution", async () => {
    let release!: (response: Response) => void
    responseGate = new Promise((resolve) => { release = resolve })
    mount(paths.chat("private-session"))
    await waitFor(() => expect(calls).toContain("GET /api/agent/session/private-session"))
    expect(calls.some((path) => /\/api\/(desktop|containers)/.test(path))).toBe(false)
    expect(sockets).toEqual([])
    await act(async () => { release(json(session)) })
    await screen.findByText("Private browser")
    await waitFor(() => expect(calls).toContain("GET /api/assistant/browser-resources/current"))
    expect(calls.some((path) => /\/api\/(desktop|containers)/.test(path))).toBe(false)
    expect(sockets.every((url) => new URL(url).searchParams.get("surface") === "assistant")).toBe(true)
    expect(calls.filter((path) => path.startsWith("POST ") && !path.includes("auth/ticket"))).toEqual([])
  })

  it("opens the private panel from the assistant topbar without provisioning on entry", async () => {
    usePanelStore.setState({ open: false })
    mount(paths.assistant)
    const toggle = await screen.findByRole("button", { name: "Open workspace panel" })
    expect(calls.some((path) => path.includes("browser-resources"))).toBe(false)
    fireEvent.click(toggle)
    await screen.findByText("Private browser")
    await screen.findByRole("button", { name: "Prepare private browser" })
    expect(calls.filter((path) => path.includes("browser-resources"))).toEqual(["GET /api/assistant/browser-resources/current"])
  })

  it("unmounts the private viewer on a later Session refusal without falling back to the shared desktop", async () => {
    const client = mount(paths.chat("private-session"))
    await screen.findByText("Private browser")
    refused = true
    await act(async () => { await client.invalidateQueries({ queryKey: ["session", "owner", "private-session"] }) })
    await waitFor(() => expect(screen.queryByText("Private browser")).toBeNull())
    expect(calls.some((path) => /\/api\/(desktop|containers)/.test(path))).toBe(false)
  })

  it("keeps the ordinary shared desktop component and its original endpoint", async () => {
    session = { ...session, visibility: "workspace", memory_policy: "standard", assistant_managed: false }
    mount(paths.chat("private-session"))
    await waitFor(() => expect(calls).toContain("GET /api/desktop/status"))
    expect(screen.queryByText("Private browser")).toBeNull()
    expect(calls.some((path) => path.includes("browser-resources"))).toBe(false)
  })
})
