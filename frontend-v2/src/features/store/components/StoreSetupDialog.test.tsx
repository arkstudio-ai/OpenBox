// The first-run store form: shows once the workspace's store list has loaded
// empty, never on a guess; 稍后 is remembered per workspace; 保存 files the
// store and the form goes away.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { ApiError, http } from "@/shared/api/http"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import type { AuthUser } from "@/shared/types/api"
import { storeSetupDismissedKey } from "../lib/setupDismissed"
import type { Store, StoreList } from "../types"
import { StoreSetupDialog } from "./StoreSetupDialog"

vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key, i18n: { language: "zh-CN" } }),
}))
vi.mock("@/shared/api/http", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/shared/api/http")>()
  return { ...actual, http: { get: vi.fn(), post: vi.fn(), patch: vi.fn() } }
})

const store: Store = {
  id: "st1",
  workspaceId: "ws",
  name: "泽岚鲜果",
  category: "food",
  categoryOpen: true,
  mainPlatforms: ["douyin_laike"],
  platformBindings: {},
  personaStatus: "none",
  createdAt: "2026-09-23T00:00:00Z",
  updatedAt: "2026-09-23T00:00:00Z",
}

function list(items: Store[]): StoreList {
  return {
    items,
    categories: ["food", "beauty", "retail", "other"],
    openCategories: ["food"],
    platforms: ["douyin_laike", "meituan_merchant"],
  }
}

const clients: QueryClient[] = []
function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  clients.push(client)
  return {
    client,
    ...render(
      <QueryClientProvider client={client}>
        <StoreSetupDialog />
      </QueryClientProvider>,
    ),
  }
}

async function settled(client: QueryClient) {
  await waitFor(() => expect(client.isFetching()).toBe(0))
}

beforeEach(() => {
  localStorage.clear()
  useAuthStore.setState({ user: { id: "u1", username: "u1", role: "user" } as AuthUser })
  useWorkspaceStore.setState({ currentId: "ws", items: [] })
  vi.mocked(http.get).mockResolvedValue(list([]))
  vi.mocked(http.post).mockResolvedValue(store)
})

afterEach(() => {
  cleanup()
  clients.splice(0).forEach((client) => client.clear())
  vi.resetAllMocks()
  localStorage.clear()
})

describe("StoreSetupDialog", () => {
  it("opens once the store list has loaded empty", async () => {
    mount()
    const dialog = await screen.findByRole("dialog")
    expect(dialog.getAttribute("aria-label")).toBe("setup.title")
    expect(http.get).toHaveBeenCalledWith("/api/stores", expect.anything())
  })

  it("stays closed when the workspace already has a store", async () => {
    vi.mocked(http.get).mockResolvedValue(list([store]))
    const { client } = mount()
    await settled(client)
    expect(screen.queryByRole("dialog")).toBeNull()
  })

  it("stays closed while the list is unknown rather than guessing", async () => {
    vi.mocked(http.get).mockResolvedValue({})
    const { client } = mount()
    await settled(client)
    expect(screen.queryByRole("dialog")).toBeNull()
  })

  it("only lets an open category be picked; the rest read as coming soon", async () => {
    mount()
    await screen.findByRole("dialog")
    const food = screen.getByRole("option", { name: "setup.categories.food" }) as HTMLOptionElement
    const closed = screen.getAllByRole("option", { name: "setup.categoryComingSoon" }) as HTMLOptionElement[]
    expect(food.disabled).toBe(false)
    expect(closed).toHaveLength(3)
    expect(closed.every((option) => option.disabled)).toBe(true)
  })

  it("稍后 closes it and remembers the skip for this workspace", async () => {
    const first = mount()
    await screen.findByRole("dialog")
    fireEvent.click(screen.getByRole("button", { name: "setup.later" }))
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull())
    expect(localStorage.getItem(storeSetupDismissedKey("ws"))).toBe("1")
    first.unmount()

    const { client } = mount()
    await settled(client)
    expect(screen.queryByRole("dialog")).toBeNull()
    expect(http.post).not.toHaveBeenCalled()
  })

  it("comes back in another workspace the skip was not made in", async () => {
    localStorage.setItem(storeSetupDismissedKey("ws"), "1")
    useWorkspaceStore.setState({ currentId: "ws2", items: [] })
    mount()
    await screen.findByRole("dialog")
  })

  it("saves the store with the chosen platforms and closes", async () => {
    mount()
    await screen.findByRole("dialog")
    const save = screen.getByRole("button", { name: "setup.save" }) as HTMLButtonElement
    expect(save.disabled).toBe(true)
    fireEvent.change(screen.getByPlaceholderText("setup.namePlaceholder"), { target: { value: " 泽岚鲜果 " } })
    fireEvent.click(screen.getByRole("checkbox", { name: "setup.platform.douyin_laike" }))
    expect(save.disabled).toBe(false)
    fireEvent.click(save)
    await waitFor(() =>
      expect(http.post).toHaveBeenCalledWith("/api/stores", {
        name: "泽岚鲜果",
        category: "food",
        main_platforms: ["douyin_laike"],
      }),
    )
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull())
    // Saving is not skipping: nothing is remembered that would hide a later prompt.
    expect(localStorage.getItem(storeSetupDismissedKey("ws"))).toBeNull()
  })

  it("closes quietly when someone else in the workspace filed the store first", async () => {
    vi.mocked(http.post).mockRejectedValue(new ApiError(409, "STORE_EXISTS", "exists"))
    mount()
    await screen.findByRole("dialog")
    fireEvent.change(screen.getByPlaceholderText("setup.namePlaceholder"), { target: { value: "店" } })
    fireEvent.click(screen.getByRole("button", { name: "setup.save" }))
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull())
    expect(screen.queryByRole("alert")).toBeNull()
  })

  it("keeps the form and shows the failure when saving fails", async () => {
    vi.mocked(http.post).mockRejectedValue(new ApiError(500, "HTTP_500", "boom"))
    mount()
    await screen.findByRole("dialog")
    fireEvent.change(screen.getByPlaceholderText("setup.namePlaceholder"), { target: { value: "店" } })
    fireEvent.click(screen.getByRole("button", { name: "setup.save" }))
    await screen.findByRole("alert")
    expect(screen.getByRole("dialog")).toBeTruthy()
  })
})
