// The cloud desktop belongs to every project conversation, including the ones
// the personal assistant hands work to; only the assistant's own page has none.
import { Suspense } from "react"
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
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
  if (path === "/api/agent/session/delegated") return session
  if (path === "/api/agent/session" || path === "/api/agent/project" || path === "/api/cron/jobs") return []
  if (path === "/api/desktop/status") return { state: "subscription_required", entitled: false }
  if (path === "/api/assistant") return { state: "ready", session: { id: "main", kind: "assistant" }, tasks: [], answers: [], unread_count: 0 }
  if (path === "/api/assistant/watch") return { items: [], has_more: false }
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
  calls = []; sockets = []
  session = { id: "delegated", user_id: "owner", workspace_id: "workspace", kind: "normal", agent: "build",
    visibility: "private", memory_policy: "assistant_isolated", assistant_managed: true, status: "idle", token_usage: {} }
  useAuthStore.setState({ accessToken: "access", user: { id: "owner", username: "owner", role: "user" }, isAuthenticated: true, isLoading: false })
  useWorkspaceStore.setState({ currentId: "workspace" })
  usePanelStore.setState({ open: true, tabs: [{ id: "desktop", kind: "desktop" }], activeTabId: "desktop" })
  vi.stubGlobal("WebSocket", Socket)
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = new URL(String(input), "http://local.test").pathname
    calls.push((init?.method ?? "GET") + " " + path)
    return json(response(path))
  }))
})
afterEach(() => {
  cleanup(); wsClient.disconnect(); vi.unstubAllGlobals()
  usePanelStore.setState({ open: false, tabs: [], activeTabId: null })
  useWorkspaceStore.getState().clear()
})

describe("cloud desktop in project conversations", () => {
  it("opens the ordinary cloud desktop in a conversation the assistant handed work to", async () => {
    mount(paths.chat("delegated"))
    await waitFor(() => expect(calls).toContain("GET /api/desktop/status"))
    expect(calls.some((path) => path.includes("browser-resources"))).toBe(false)
    // A socket, when one opens, provisions the shared workspace runtime like any project chat.
    expect(sockets.every((url) => new URL(url).searchParams.get("surface") === "workspace")).toBe(true)
  })

  it("keeps the desktop for an ordinary workspace conversation", async () => {
    session = { ...session, visibility: "workspace", memory_policy: "standard", assistant_managed: false }
    mount(paths.chat("delegated"))
    await waitFor(() => expect(calls).toContain("GET /api/desktop/status"))
  })

  it("gives the assistant's own page no panel, toggle or desktop", async () => {
    usePanelStore.setState({ open: false })
    mount(paths.assistant)
    await screen.findByText("Assistant")
    await waitFor(() => expect(calls).toContain("GET /api/workspaces"))
    expect(screen.queryByRole("button", { name: "Open workspace panel" })).toBeNull()
    expect(calls.some((path) => /\/api\/(desktop|containers)/.test(path))).toBe(false)
    expect(sockets.every((url) => new URL(url).searchParams.get("surface") === "assistant")).toBe(true)
  })

  it("opens the panel from a project conversation's top bar", async () => {
    usePanelStore.setState({ open: false })
    mount(paths.chat("delegated"))
    fireEvent.click(await screen.findByRole("button", { name: "Open workspace panel" }))
    await waitFor(() => expect(calls).toContain("GET /api/desktop/status"))
  })
})
