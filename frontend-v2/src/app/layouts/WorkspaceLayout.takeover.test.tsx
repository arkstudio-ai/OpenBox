// Settings and the admin console replace the workspace shell instead of sitting
// inside it. Proof at the layout level — through the real shell, with the pages
// themselves stubbed — that the workspace sidebar and the workbench panel step
// aside there, and that ordinary centre pages keep both.
import { Suspense } from "react"
import { cleanup, render, waitFor } from "@testing-library/react"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { createMemoryRouter, RouterProvider } from "react-router"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

// The appearance store reads `matchMedia` while its module evaluates, so these
// have to exist before the imports below run — `beforeEach` is already too late.
vi.hoisted(() => {
  const define = (name: string, value: unknown) =>
    Object.defineProperty(globalThis, name, { value, writable: true, configurable: true })
  define("matchMedia", (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
    addListener: () => undefined,
    removeListener: () => undefined,
    dispatchEvent: () => false,
  }))
  define(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  )
})

import "@/shared/i18n"
import WorkspaceLayout from "@/app/layouts/WorkspaceLayout"
import { usePanelStore } from "@/features/workbench"
import { useAuthStore } from "@/shared/api/auth-store"
import { paths, routePatterns } from "@/shared/router/paths"
import { wsClient } from "@/shared/ws/client"
import type { AuthUser } from "@/shared/types/api"

class FakeSocket {
  static readonly OPEN = 1
  readyState = 0
  onopen: (() => void) | null = null
  onmessage: ((event: { data: string }) => void) | null = null
  onclose: ((event?: { code: number }) => void) | null = null
  onerror: (() => void) | null = null
  close(): void {
    this.readyState = 3
    this.onclose?.()
  }
  send(): void {}
}

function responseFor(path: string): unknown {
  if (path === "/api/workspaces") {
    return {
      items: [{ id: "ws", name: "Mine", owner_user_id: "u1", kind: "personal", role: "owner" }],
      default_workspace_id: "ws",
    }
  }
  if (path === "/api/agent/project" || path === "/api/agent/session") return []
  if (path === "/api/billing/balance") return { balance: 10 }
  return {}
}

const page = (name: string) => <p>{name}</p>

function mount(entry: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const memory = createMemoryRouter(
    [
      {
        path: paths.app,
        element: <WorkspaceLayout />,
        children: [
          { index: true, element: page("chat") },
          { path: routePatterns.chat, element: page("chat") },
          { path: routePatterns.settings, element: page("settings") },
          { path: routePatterns.cron, element: page("cron") },
          { path: `${routePatterns.admin}/*`, element: page("admin") },
        ],
      },
    ],
    { initialEntries: [entry] },
  )
  const { container } = render(
    <QueryClientProvider client={client}>
      <Suspense fallback={null}>
        <RouterProvider router={memory} />
      </Suspense>
    </QueryClientProvider>,
  )
  return container
}

/**
 * The shell's own children, so a nested `aside`/`section` cannot stand in.
 * Keyed off `main`, which the shell renders only once the workspace list has
 * resolved — the loading placeholder it renders until then is also a
 * full-height flex box, and matching that would find no sidebar on any page.
 */
async function shell(container: HTMLElement) {
  const main = await waitFor(() => {
    const el = container.querySelector("main")
    expect(el).not.toBeNull()
    return el!
  })
  const root = main.parentElement!
  return {
    sidebar: root.querySelector(":scope > aside"),
    panel: root.querySelector(":scope > section"),
  }
}

beforeEach(() => {
  vi.stubGlobal("WebSocket", FakeSocket)
  vi.stubGlobal("fetch", async (input: RequestInfo | URL) => {
    const raw = typeof input === "string" ? input : input instanceof URL ? input.href : input.url
    const { pathname } = new URL(raw, "http://app.test")
    return new Response(JSON.stringify(responseFor(pathname)), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    })
  })
  // Open, so a page that still hosts the panel actually shows one.
  usePanelStore.setState({ open: true, tabs: [{ id: "t1", kind: "menu" }], activeTabId: "t1" })
  useAuthStore.setState({
    accessToken: "token",
    user: { id: "u1", username: "root", role: "admin" } as AuthUser,
    isAuthenticated: true,
    isLoading: false,
  })
})

afterEach(() => {
  cleanup()
  wsClient.disconnect()
  vi.unstubAllGlobals()
  usePanelStore.setState({ open: false, tabs: [], activeTabId: null })
  useAuthStore.setState({ accessToken: null, user: null, isAuthenticated: false, isLoading: false })
})

describe("takeover pages", () => {
  it.each([
    ["settings", paths.settings()],
    ["settings tab", paths.settings("appearance")],
    ["admin console", paths.adminFleet],
    ["trajectory viewer", paths.adminTrajectories()],
  ])("%s drops the workspace sidebar and the workbench panel", async (_name, entry) => {
    const { sidebar, panel } = await shell(mount(entry))
    expect(sidebar).toBeNull()
    expect(panel).toBeNull()
  })
})

describe("everywhere else", () => {
  it.each([
    ["the chat surface", paths.app],
    ["a conversation", paths.chat("s1")],
    ["an ordinary centre page", paths.cron],
  ])("%s keeps the sidebar", async (_name, entry) => {
    const { sidebar } = await shell(mount(entry))
    expect(sidebar).not.toBeNull()
  })

  it("keeps the workbench panel on a conversation", async () => {
    const { panel } = await shell(mount(paths.chat("s1")))
    expect(panel).not.toBeNull()
  })
})
