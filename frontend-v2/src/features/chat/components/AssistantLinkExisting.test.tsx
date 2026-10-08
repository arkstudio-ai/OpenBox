import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { afterEach, beforeEach, expect, it, vi } from "vitest"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import { AssistantLinkExisting } from "./AssistantLinkExisting"
import { linkExisting } from "../lib/link-existing"

vi.mock("react-i18next", () => ({ useTranslation: () => ({ t: (key: string) => key }) }))
vi.mock("@/shared/hooks/useApiErrorMessage", () => ({ useApiErrorMessage: () => () => "Unavailable" }))
let fetchMock: ReturnType<typeof vi.fn>
let number = 0
let user: string
const version = "a".repeat(64)
const item = { id: "original-session", title: "Existing work", project_id: "p", project_name: "Project A",
  link: { available: true, reason_code: null, version, task_id: null, archived: false } }
const response = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status })
const receipt = { task_id: "linked-task", command_id: "link-command", execution_session_id: item.id, state: "linked" }
beforeEach(() => {
  user = `link-owner-${++number}`
  sessionStorage.clear()
  useAuthStore.setState({ user: { id: user } as never, accessToken: null })
  useWorkspaceStore.setState({ currentId: "link-workspace" })
  fetchMock = vi.fn().mockImplementation((_url, init) => Promise.resolve(response(init?.method === "POST"
    ? receipt : { items: [item], next_cursor: null })))
  vi.stubGlobal("fetch", fetchMock)
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })
function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(<QueryClientProvider client={client}><AssistantLinkExisting /></QueryClientProvider>)
}

it("lists the user's conversations once shown, then links the exact displayed original without input", async () => {
  mount()
  await screen.findByText(item.title)
  expect(screen.getByText("Project A")).toBeTruthy()
  fireEvent.click(screen.getByRole("button", { name: "assistant.link.action" }))
  await screen.findByRole("status")
  await waitFor(() => expect(fetchMock.mock.calls.filter(([, options]) => options.method === "POST")).toHaveLength(1))
  const [url, options] = fetchMock.mock.calls.find(([, options]) => options.method === "POST")!
  expect(url).toContain("/api/assistant/tasks/link")
  expect(JSON.parse(options.body)).toEqual({ session_id: item.id, expected_version: version, idempotency_key: expect.any(String) })
  expect(new Headers(options.headers).get("X-Workspace-Id")).toBe("link-workspace")
})

it("explains why a subagent conversation cannot be linked and never sends a link", async () => {
  fetchMock.mockResolvedValueOnce(response({ items: [{ ...item, link: { ...item.link, available: false,
    reason_code: "ASSISTANT_LINK_CHILD" } }], next_cursor: null }))
  mount()
  await screen.findByText("assistant.link.reasons.ASSISTANT_LINK_CHILD")
  const button = screen.getByRole("button", { name: "assistant.link.action" }) as HTMLButtonElement
  expect(button.disabled).toBe(true)
  fireEvent.click(button)
  expect(fetchMock).toHaveBeenCalledTimes(1)
})

it("reconciles a lost response with the original key and observation even after reload", async () => {
  fetchMock.mockRejectedValueOnce(new TypeError("response lost"))
  await expect(linkExisting(user, "link-workspace", item.id, version)).rejects.toThrow("response lost")
  const firstBody = fetchMock.mock.calls[0][1].body
  expect(sessionStorage.length).toBe(1)
  vi.resetModules()
  // Storage contains the full pinned request, not just a new-version fingerprint.
  expect(JSON.parse(sessionStorage.getItem(sessionStorage.key(0)!)!)).toEqual(JSON.parse(firstBody))
  const [{ linkExisting: reloaded }, { useAuthStore: auth }, { useWorkspaceStore: workspace }] = await Promise.all([
    import("../lib/link-existing"), import("@/shared/api/auth-store"), import("@/shared/api/workspace-store"),
  ])
  auth.setState({ user: { id: user } as never, accessToken: null })
  workspace.setState({ currentId: "link-workspace" })
  await reloaded(user, "link-workspace", item.id, "b".repeat(64))
  expect(fetchMock.mock.calls[1][1].body).toEqual(firstBody)
  expect(sessionStorage.length).toBe(0)
})

it("does not show a late success in a different workspace", async () => {
  let resolve!: (value: Response) => void
  fetchMock.mockImplementation((_url, options) => options.method === "POST"
    ? new Promise<Response>((done) => { resolve = done }) : Promise.resolve(response({ items: [item], next_cursor: null })))
  mount()
  fireEvent.click(await screen.findByRole("button", { name: "assistant.link.action" }))
  await waitFor(() => expect(resolve).toBeTypeOf("function"))
  act(() => useWorkspaceStore.setState({ currentId: "other-workspace" }))
  await act(async () => resolve(response(receipt)))
  expect(screen.queryByText("assistant.link.success")).toBeNull()
  expect(sessionStorage.length).toBe(1)
})

it("never shows a conversation's id, even for an untitled one", async () => {
  fetchMock.mockResolvedValueOnce(response({ items: [{ ...item, title: "" }], next_cursor: null }))
  const { container } = mount()
  await screen.findByText("assistant.link.untitled")
  expect(container.textContent).not.toContain(item.id)
})

it("rejects a mismatched receipt and retains the original retry identity", async () => {
  fetchMock.mockResolvedValueOnce(response({ ...receipt, execution_session_id: "replacement" }))
  await expect(linkExisting(user, "link-workspace", item.id, version)).rejects.toThrow("Invalid link receipt")
  await linkExisting(user, "link-workspace", item.id, "b".repeat(64))
  expect(fetchMock.mock.calls[1][1].body).toEqual(fetchMock.mock.calls[0][1].body)
})

it("keeps the server's newest-first order across pages and labels shared and watched conversations", async () => {
  const shared = { ...item, id: "s3", title: "Newest shared", visibility: "workspace", watched: false }
  const watching = { ...item, id: "s2", title: "Already watched", visibility: "private", watched: true, task_id: "t2",
    link: { ...item.link, task_id: "t2" } }
  const archived = { ...item, id: "s1", title: "Oldest", visibility: "private", watched: false, task_id: "t1",
    link: { ...item.link, task_id: "t1", archived: true } }
  fetchMock.mockImplementation((url: string) => Promise.resolve(response(url.includes("cursor=s2")
    ? { items: [archived], next_cursor: null } : { items: [shared, watching], next_cursor: "s2" })))
  mount()
  await screen.findByText("Newest shared")
  fireEvent.click(screen.getByRole("button", { name: "assistant.link.more" }))
  await screen.findByText("Oldest")
  const rows = ["Newest shared", "Already watched", "Oldest"].map((title) => screen.getByText(title).closest("div.rounded-xl") as HTMLElement)
  expect(rows[0].compareDocumentPosition(rows[1]) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  expect(rows[1].compareDocumentPosition(rows[2]) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  const labels = (row: HTMLElement) => ["assistant.link.workspaceVisible", "assistant.link.watched"]
    .filter((label) => row.textContent!.includes(label))
  expect(rows.map(labels)).toEqual([["assistant.link.workspaceVisible"], ["assistant.link.watched"], []])
  // Watched: nothing to do here. Shared: may be watched like any other. Archived: watch again.
  expect(rows[1].querySelector("button")).toBeNull()
  expect(rows[0].querySelector("button")!.textContent).toBe("assistant.link.action")
  expect((rows[0].querySelector("button") as HTMLButtonElement).disabled).toBe(false)
  expect(rows[2].querySelector("button")!.textContent).toBe("assistant.link.reopen")
  expect(fetchMock.mock.calls.map(([url]) => new URL(url, "http://local.test").searchParams.get("cursor"))).toEqual([null, "s2"])
})
