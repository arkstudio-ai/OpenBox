import type { PropsWithChildren } from "react"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { MemoryRouter, Route, Routes } from "react-router"
import { afterEach, beforeEach, expect, it, vi } from "vitest"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import type { Session } from "@/shared/types/api"
import { putToOss } from "@/features/chat/api/assets"
import ChatRoute, { ChatSessionView } from "./ChatRoute"

const { send, events } = vi.hoisted(() => ({ send: vi.fn(), events: vi.fn() }))
vi.mock("react-i18next", async (importOriginal) => ({
  ...await importOriginal<typeof import("react-i18next")>(),
  // The composer's AI disclosure links the legal page in the current language.
  useTranslation: () => ({ t: (key: string) => key, i18n: { language: "zh-CN" } }),
}))
// Keep the real route, Session query, Composer, attachments and HTTP adapters.
// Transcript/socket/request orchestration is unrelated to uploading a draft.
vi.mock("@/features/chat", async (importOriginal) => ({
  ...await importOriginal<typeof import("@/features/chat")>(),
  ChatFlow: () => null,
  useChatEvents: events,
  useSendChat: () => send,
  useAbortSession: () => ({ isPending: false, mutate: vi.fn() }),
  usePermissionsQuery: () => ({}),
  useQuestionsQuery: () => ({}),
}))
vi.mock("@/features/chat/hooks/useChatHistory", () => ({
  useChatHistory: () => ({ messagesQ: {}, hasMore: false, loadingOlder: false, loadOlder: vi.fn() }),
}))
vi.mock("@/features/resources", () => ({ useResourceMention: () => undefined }))
vi.mock("@/features/chat/api/assets", async (importOriginal) => ({
  ...await importOriginal<typeof import("@/features/chat/api/assets")>(),
  putToOss: vi.fn(async () => undefined),
}))

const actor = { id: "attachment-owner", username: "owner", role: "user" }
const session: Session = {
  id: "attachment-session", user_id: actor.id, workspace_id: "workspace",
  title: "Execution", agent: "build", model: "test/model", status: "idle",
  created_at: "2026-10-05T00:00:00Z", updated_at: "2026-10-05T00:00:00Z",
  visibility: "private", memory_policy: "assistant_isolated", kind: "normal", assistant_managed: true,
}
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
  status, headers: { "Content-Type": "application/json" },
})
let client: QueryClient
let calls: Array<{ path: string; init?: RequestInit }>
let sessionReply: () => Promise<Response>
let assetStatus: number

function wrapper({ children }: PropsWithChildren) {
  return <QueryClientProvider client={client}><MemoryRouter initialEntries={[`/chat/${session.id}`]}>{children}</MemoryRouter></QueryClientProvider>
}

function upload(container: HTMLElement) {
  const input = container.querySelector('input[type="file"]')
  expect(input).not.toBeNull()
  fireEvent.change(input!, { target: { files: [new File(["private draft bytes"], "draft.txt", { type: "text/plain" })] } })
}

function assetCalls() {
  return calls.filter(({ path }) => path === "/api/assets")
}

beforeEach(() => {
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  useAuthStore.getState().setAuth("test-token", actor)
  useWorkspaceStore.getState().setCurrent("workspace")
  calls = []
  assetStatus = 200
  sessionReply = async () => json(session)
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const path = new URL(url, "http://local.test").pathname
    calls.push({ path, init })
    if (path === `/api/agent/session/${session.id}`) return sessionReply()
    if (path === "/api/containers") return json({ containers: [{ id: "shared", name: "shared", status: "running" }] })
    if (path === "/api/agent/agent") return json([{ name: "build" }, { name: "plan" }])
    if (path === "/api/agent/config" || path === "/api/auth/me/preferences") return json({})
    if (path === "/api/agent/skill" || path === "/api/agent/command") return json([])
    if (path === "/api/assets") return assetStatus === 200
      ? json({ id: "asset-draft", name: "draft.txt", sandboxPath: "/workspace/uploads/draft.txt", putUrl: "https://object.invalid/upload", headers: {} })
      : json({ detail: "OSS unavailable" }, assetStatus)
    if (path === "/api/assets/asset-draft/complete") return json({ id: "asset-draft", name: "draft.txt", sandboxPath: "/workspace/uploads/draft.txt" })
    if (path === "/api/containers/shared/files/upload") return json({ path: "/workspace/uploads/draft.txt" })
    throw new Error(`Unexpected test request: ${path}`)
  }))
})

afterEach(() => {
  cleanup()
  client.clear()
  useAuthStore.getState().clearAuth()
  useWorkspaceStore.getState().clear()
  vi.clearAllMocks()
  vi.unstubAllGlobals()
})

it("binds an execution route upload while retaining its build/plan composer and send contract", async () => {
  const { container } = render(<Routes><Route path="/chat/:sessionId" element={<ChatRoute />} /></Routes>, { wrapper })
  await screen.findByRole("button", { name: "mode.label" })
  expect(screen.queryByText("mode.assistant")).toBeNull()
  upload(container)
  await waitFor(() => expect(calls.some(({ path }) => path.endsWith("/complete"))).toBe(true))
  expect(JSON.parse(String(assetCalls()[0].init?.body)).session_id).toBe(session.id)
  expect(calls.some(({ path }) => path.startsWith("/api/containers"))).toBe(false)
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "continue execution" } })
  await waitFor(() => expect((screen.getByRole("button", { name: "send" }) as HTMLButtonElement).disabled).toBe(false))
  fireEvent.click(screen.getByRole("button", { name: "send" }))
  expect(send).toHaveBeenCalledExactlyOnceWith("continue execution\n\n[attachments]\n- /workspace/uploads/draft.txt", expect.objectContaining({ agent: "build", attachments: ["asset-draft"] }))
  expect(events).toHaveBeenCalledWith(session.id, "workspace")
})

it("blocks even the file input callback until Session metadata resolves, then uses the private audience", async () => {
  let reply!: (response: Response) => void
  sessionReply = () => new Promise<Response>((resolve) => { reply = resolve })
  const { container } = render(<ChatSessionView sessionId={session.id} />, { wrapper })
  upload(container)
  expect(assetCalls()).toEqual([])
  expect(calls.some(({ path }) => path.startsWith("/api/containers"))).toBe(false)
  await act(async () => { reply(json(session)) })
  await waitFor(() => expect((screen.getByTestId("composer-tools") as HTMLButtonElement).disabled).toBe(false))
  upload(container)
  await waitFor(() => expect(putToOss).toHaveBeenCalledOnce())
  expect(JSON.parse(String(assetCalls()[0].init?.body)).session_id).toBe(session.id)
})

it("does not upload using retained Session data when its fresh query fails", async () => {
  client.setQueryData(["session", actor.id, session.id], session)
  sessionReply = async () => json({ detail: "Session unavailable" }, 403)
  const { container } = render(<ChatSessionView sessionId={session.id} />, { wrapper })
  await waitFor(() => expect(client.getQueryState(["session", actor.id, session.id])?.status).toBe("error"))
  upload(container)
  expect(assetCalls()).toEqual([])
  expect(putToOss).not.toHaveBeenCalled()
})

it("keeps main-assistant attachments in the asset service without adding sandbox paths to the request", async () => {
  sessionReply = async () => json({ ...session, kind: "assistant", agent: "assistant" })
  const { container } = render(<ChatSessionView sessionId={session.id} assistant />, { wrapper })
  await waitFor(() => expect((screen.getByTestId("composer-tools") as HTMLButtonElement).disabled).toBe(false))
  upload(container)
  await waitFor(() => expect(calls.some(({ path }) => path.endsWith("/complete"))).toBe(true))
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "read my file" } })
  await waitFor(() => expect((screen.getByRole("button", { name: "send" }) as HTMLButtonElement).disabled).toBe(false))
  fireEvent.click(screen.getByRole("button", { name: "send" }))
  expect(send).toHaveBeenCalledExactlyOnceWith("read my file", expect.objectContaining({ agent: "assistant", attachments: ["asset-draft"] }))
  expect(JSON.parse(String(assetCalls()[0].init?.body)).session_id).toBe(session.id)
  expect(calls.some(({ path }) => path.startsWith("/api/containers"))).toBe(false)
})

it("keeps the ordinary shared route's legacy upload when OSS is unavailable", async () => {
  sessionReply = async () => json({ ...session, visibility: "workspace", memory_policy: "standard", assistant_managed: false })
  assetStatus = 503
  const { container } = render(<Routes><Route path="/chat/:sessionId" element={<ChatRoute />} /></Routes>, { wrapper })
  await waitFor(() => expect((screen.getByTestId("composer-tools") as HTMLButtonElement).disabled).toBe(false))
  upload(container)
  await waitFor(() => expect(calls.some(({ path }) => path.endsWith("/files/upload"))).toBe(true))
  expect(JSON.parse(String(assetCalls()[0].init?.body))).not.toHaveProperty("session_id")
})
