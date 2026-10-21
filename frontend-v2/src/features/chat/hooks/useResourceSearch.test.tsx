import type { PropsWithChildren } from "react"
import { act, cleanup, renderHook, waitFor } from "@testing-library/react"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { afterEach, describe, expect, it, vi } from "vitest"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import type { MentionScope } from "./useMentionMenu"
import { useResourceSearch } from "./useResourceSearch"

const asset = { id: "old-image", name: "IMG_001.png", description: "红色圆形", mime: "image/png",
  kind: "image", size: 100, sandboxPath: "/workspace/IMG_001.png", url: "https://assets.example/image" }
const base: MentionScope = { projects: [], project: "all", source: "all", setProject: vi.fn(), setSource: vi.fn(), items: [asset], loading: false }

function mount(scope = base, query = "红色圆形") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const wrapper = ({ children }: PropsWithChildren) => <QueryClientProvider client={client}>{children}</QueryClientProvider>
  return { client, ...renderHook((props) => useResourceSearch(props.scope, props.query, true), {
    initialProps: { scope, query }, wrapper,
  }) }
}

afterEach(() => {
  cleanup()
  useWorkspaceStore.setState({ currentId: null })
  useAuthStore.setState({ user: null })
})

describe("resource picker search", () => {
  it("matches descriptions when a caller only supplies local resources", () => {
    const { result } = mount()
    expect(result.current.items).toEqual([asset])
  })

  it("finds older server matches absent from the recent browsing list", async () => {
    const search = vi.fn().mockResolvedValue([asset])
    const { result } = mount({ ...base, items: [], search })
    await waitFor(() => expect(result.current.items).toEqual([asset]))
    expect(search).toHaveBeenCalledWith("红色圆形", expect.any(AbortSignal))
  })

  it("hides the previous query's result immediately and debounces the next request", async () => {
    const search = vi.fn().mockImplementation(async (q) => q === "红色圆形" ? [asset] : [])
    const scope = { ...base, items: [], search }
    const { result, rerender } = mount(scope)
    await waitFor(() => expect(result.current.items).toHaveLength(1))
    rerender({ scope, query: "blue" })
    expect(result.current.items).toEqual([])
    expect(result.current.loading).toBe(true)
    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(result.current.items).toEqual([])
    expect(search).toHaveBeenCalledTimes(2)
  })

  it("does not reuse results across project, source, workspace or account changes", async () => {
    const search = vi.fn().mockResolvedValue([asset])
    const scope = { ...base, items: [], search }
    const { result, rerender } = mount(scope)
    await waitFor(() => expect(result.current.items).toHaveLength(1))
    const empty = vi.fn().mockResolvedValue([])
    for (const next of [{ ...scope, project: "other", search: empty }, { ...scope, source: "agent" as const, search: empty }]) {
      rerender({ scope: next, query: "红色圆形" })
      expect(result.current.items).toEqual([])
      await waitFor(() => expect(result.current.loading).toBe(false))
    }
    rerender({ scope, query: "红色圆形" })
    expect(result.current.items).toHaveLength(1)
    search.mockResolvedValue([])
    act(() => useWorkspaceStore.setState({ currentId: "workspace-2" }))
    expect(result.current.items).toEqual([])
    await waitFor(() => expect(result.current.loading).toBe(false))
    search.mockResolvedValue([asset])
    act(() => useAuthStore.setState({ user: { id: "user-2" } as NonNullable<ReturnType<typeof useAuthStore.getState>["user"]> }))
    await waitFor(() => expect(result.current.items).toHaveLength(1))
    expect(search).toHaveBeenCalledTimes(3)
  })

  it("refreshes search results when a description or upload invalidates resources", async () => {
    const search = vi.fn().mockResolvedValue([asset])
    const { result, client } = mount({ ...base, search })
    await waitFor(() => expect(result.current.items).toHaveLength(1))
    search.mockResolvedValue([])
    await act(async () => { await client.invalidateQueries({ queryKey: ["resources", "anonymous"] }) })
    await waitFor(() => expect(result.current.items).toEqual([]))
  })

  it("reports failed searches instead of claiming that no resources match", async () => {
    const { result } = mount({ ...base, search: vi.fn().mockRejectedValue(new Error("unavailable")) })
    await waitFor(() => expect(result.current.error).toBe(true))
    expect(result.current.loading).toBe(false)
  })
})
