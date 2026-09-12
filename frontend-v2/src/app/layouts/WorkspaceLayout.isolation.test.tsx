// Route-level proof, through the real router and the real workspace shell, that
// opening the trajectory viewer does not act as — or for — anyone: no agent
// socket (which provisions the viewer's sandbox on connect), no ticket for it,
// no desktop/sandbox/cron/workbench or chat execution calls, no writes, and the
// inspected session never reaches ordinary session state or APIs.
import { act, cleanup, render, screen, waitFor } from "@testing-library/react"
import { Suspense } from "react"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { createMemoryRouter, RouterProvider } from "react-router"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import "@/shared/i18n"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceUi } from "@/features/workspace"
import { router } from "@/app/router/router"
import { paths } from "@/shared/router/paths"
import { wsClient } from "@/shared/ws/client"
import type { AuthUser } from "@/shared/types/api"

vi.mock("@/features/admin-trajectories", async () => {
  const list = await import("@/features/admin-trajectories/components/TrajectoryListPage")
  const access = await import("@/features/admin-trajectories/hooks/useTrajectoryAccessWatcher")
  return {
    TrajectoryListPage: list.TrajectoryListPage,
    // The shell is under test here; the session page's own reads are covered by its tests.
    TrajectorySessionPage: ({ sessionId }: { sessionId: string }) => <p>{`detail:${sessionId}`}</p>,
    useTrajectoryAccessWatcher: access.useTrajectoryAccessWatcher,
  }
})

class FakeSocket {
  static readonly CONNECTING = 0
  static readonly OPEN = 1
  static readonly CLOSED = 3
  readonly url: string
  readyState = FakeSocket.CONNECTING
  onopen: (() => void) | null = null
  onmessage: ((event: { data: string }) => void) | null = null
  onclose: ((event?: { code: number }) => void) | null = null
  onerror: (() => void) | null = null
  constructor(url: string) {
    this.url = url
    sockets.push(this)
  }
  close(): void {
    this.readyState = FakeSocket.CLOSED
    this.onclose?.()
  }
  send(): void {}
}

interface Call {
  method: string
  path: string
}

let sockets: FakeSocket[]
let calls: Call[]
const TARGET = "session_user_b"

// The viewer's own side-effect-free shell reads, never scoped to the inspected
// session: workspace list, the viewer's projects and session list (the topbar
// names the page and resolves its way back with them), appearance preferences
// and the environment badge. Deliberately absent:
// `/api/billing/balance` — its handler settles the billing period
// (ensure_period_allowance), a write, so the shell must not issue it here.
const ALLOWED_READS = [
  /^\/api\/workspaces$/,
  /^\/api\/agent\/project$/,
  /^\/api\/agent\/session$/,
  /^\/api\/auth\/me\/preferences$/,
  /^\/api\/environment$/,
  /^\/api\/admin\/trajectories\//,
]

function responseFor(path: string): unknown {
  if (path === "/api/workspaces") {
    return {
      items: [{ id: "ws_admin", name: "Admin", owner_user_id: "admin-a", kind: "personal", role: "owner" }],
      default_workspace_id: "ws_admin",
    }
  }
  if (path === "/api/agent/project" || path === "/api/agent/session") return []
  if (path.startsWith("/api/admin/trajectories/sessions"))
    return { items: [], next_cursor: null, has_more: false }
  if (path === "/api/auth/ticket") return { ticket: "agent-ticket" }
  return {}
}

function pathOf(input: RequestInfo | URL): string {
  const raw = typeof input === "string" ? input : input instanceof URL ? input.href : input.url
  return new URL(raw, "http://app.test").pathname
}

function mount(entry: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const memory = createMemoryRouter(router.routes, { initialEntries: [entry] })
  render(
    <QueryClientProvider client={client}>
      <Suspense fallback={null}>
        <RouterProvider router={memory} />
      </Suspense>
    </QueryClientProvider>,
  )
  return memory
}

function expectNoViewerExecution() {
  expect(sockets.filter((socket) => socket.url.includes("/ws/agent"))).toEqual([])
  expect(calls.filter((call) => call.method !== "GET")).toEqual([])
  expect(calls.filter((call) => !ALLOWED_READS.some((pattern) => pattern.test(call.path)))).toEqual([])
}

beforeEach(() => {
  sockets = []
  calls = []
  vi.stubGlobal("WebSocket", FakeSocket)
  // jsdom has neither; the shell's responsive sidebar and panels use both.
  vi.stubGlobal("matchMedia", (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
    addListener: () => undefined,
    removeListener: () => undefined,
    dispatchEvent: () => false,
  }))
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  )
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = pathOf(input)
      calls.push({ method: (init?.method ?? "GET").toUpperCase(), path })
      return new Response(JSON.stringify(responseFor(path)), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      })
    }),
  )
  useWorkspaceUi.setState({ lastSessionId: "own_chat" })
  useAuthStore.setState({
    accessToken: "admin-access",
    user: { id: "admin-a", username: "admin", role: "admin" } as AuthUser,
    isAuthenticated: true,
    isLoading: false,
  })
})

afterEach(() => {
  cleanup()
  wsClient.disconnect()
  vi.unstubAllGlobals()
  useAuthStore.setState({ accessToken: null, user: null, isAuthenticated: false, isLoading: false })
})

describe("trajectory routes inside the workspace shell", () => {
  it("list: reads only admin trajectory data and the viewer's own navigation", async () => {
    mount(paths.adminTrajectories())
    await waitFor(
      () => expect(calls.some((call) => call.path === "/api/admin/trajectories/sessions")).toBe(true),
      { timeout: 8_000 },
    )
    // Let every layout effect that would open a socket or fetch run.
    await act(() => new Promise((resolve) => setTimeout(resolve, 50)))
    expectNoViewerExecution()
  }, 15_000)

  it("detail: never opens the agent socket or hands the target to ordinary session state", async () => {
    mount(paths.adminTrajectorySession(TARGET))
    await screen.findByText(`detail:${TARGET}`, undefined, { timeout: 8_000 })
    await waitFor(() => expect(calls.some((call) => call.path === "/api/workspaces")).toBe(true))
    await act(() => new Promise((resolve) => setTimeout(resolve, 50)))
    expectNoViewerExecution()
    expect(
      calls.filter((call) => call.path.includes(TARGET) && !call.path.startsWith("/api/admin/trajectories/")),
    ).toEqual([])
    expect(useWorkspaceUi.getState().lastSessionId).toBe("own_chat")
  }, 15_000)

  it("still connects the viewer's own realtime channel elsewhere, and drops it on entering the viewer", async () => {
    const memory = mount(paths.adminTrajectories())
    await waitFor(
      () => expect(calls.some((call) => call.path === "/api/admin/trajectories/sessions")).toBe(true),
      { timeout: 8_000 },
    )
    expect(sockets).toEqual([])
    await act(() => memory.navigate(paths.settings()))
    await waitFor(() => expect(sockets.some((socket) => socket.url.includes("/ws/agent"))).toBe(true), {
      timeout: 8_000,
    })
    expect(calls.some((call) => call.method === "POST" && call.path === "/api/auth/ticket")).toBe(true)
    // Settings takes the window over and renders no sidebar, so the credit
    // balance belongs to a page that still has one. The viewer omits it even
    // there, which is what the whole-suite `expectNoViewerExecution` asserts.
    await act(() => memory.navigate(paths.app))
    await waitFor(() => expect(calls.some((call) => call.path === "/api/billing/balance")).toBe(true))
    await act(() => memory.navigate(paths.adminTrajectories()))
    await waitFor(() => expect(sockets.every((socket) => socket.readyState === FakeSocket.CLOSED)).toBe(true))
  }, 20_000)
})
