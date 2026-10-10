import type { PropsWithChildren } from "react"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { act, cleanup, renderHook, waitFor } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { useAuthStore } from "@/shared/api/auth-store"
import { containerKeys } from "@/shared/api/containers"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import type { Session } from "@/shared/types/api"
import { putToOss } from "../api/assets"
import { useComposerAttachments } from "./useComposerAttachments"

// Only the external object-store transfer is replaced. Asset creation and
// completion use the real HTTP adapter; the fetch boundary records every call.
vi.mock("../api/assets", async (importOriginal) => ({
  ...await importOriginal<typeof import("../api/assets")>(),
  putToOss: vi.fn(async () => undefined),
}))

const actor = { id: "u-owner", username: "owner", role: "user" }
const desktop = { id: "shared-desktop", name: "shared", status: "running" }
const shared: Session = {
  id: "execution-session", user_id: actor.id, workspace_id: "workspace",
  title: "Execution", agent: "build", model: "test/model", status: "idle",
  created_at: "2026-10-05T00:00:00Z", updated_at: "2026-10-05T00:00:00Z",
  visibility: "workspace", memory_policy: "standard", kind: "normal", assistant_managed: false,
}
const privateSession: Session = { ...shared, visibility: "private" }
const file = () => new File(["private attachment bytes"], "private.txt", { type: "text/plain" })
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
  status, headers: { "Content-Type": "application/json" },
})
let client: QueryClient
let calls: Array<{ path: string; init?: RequestInit }>
let createReply: () => Promise<Response>

function wrapper({ children }: PropsWithChildren) {
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>
}

function uploadCalls() {
  return calls.filter(({ path }) => path === "/api/assets" || path.endsWith("/files/upload"))
}

function hook(session: Session | undefined = privateSession, assistant = false, sessionId: string | undefined = shared.id) {
  return renderHook(({ current }) => useComposerAttachments(assistant, sessionId, current), {
    initialProps: { current: session as Session | undefined }, wrapper,
  })
}

beforeEach(() => {
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  useAuthStore.getState().setAuth("test-token", actor)
  useWorkspaceStore.getState().setCurrent("workspace")
  calls = []
  createReply = async () => json({ id: "asset-1", name: "private.txt", sandboxPath: "/workspace/uploads/private.txt", putUrl: "https://object.invalid/upload", headers: {} })
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const path = new URL(url, "http://local.test").pathname
    calls.push({ path, init })
    if (path === "/api/containers") return json({ containers: [desktop] })
    if (path === "/api/assets") return createReply()
    if (path === "/api/assets/asset-1/complete") return json({ id: "asset-1", name: "private.txt", sandboxPath: "/workspace/uploads/private.txt" })
    if (path === "/api/containers/shared-desktop/files/upload") return json({ path: "/workspace/uploads/private.txt" })
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

describe("private composer asset audience", () => {
  it.each([
    ["private execution", privateSession, false],
    ["managed execution", { ...shared, assistant_managed: true }, false],
    ["isolated descendant", { ...shared, memory_policy: "assistant_isolated" }, false],
    ["main assistant", { ...privateSession, kind: "assistant" }, true],
  ] as const)("binds %s on the initial create without looking up a shared desktop", async (_label, session, assistant) => {
    client.setQueryData(containerKeys.all(actor.id), { containers: [desktop] })
    const { result } = hook(session, assistant)
    await waitFor(() => expect(result.current.canAttach).toBe(true))
    act(() => result.current.attachments.addFiles([file()]))
    await waitFor(() => expect(result.current.attachments.items[0]?.status).toBe("done"))
    const request = uploadCalls()[0]
    expect(JSON.parse(String(request.init?.body))).toEqual({ name: "private.txt", mime: "text/plain", size: file().size, session_id: session.id })
    expect(new Headers(request.init?.headers).get("X-Workspace-Id")).toBe("workspace")
    expect(result.current.containerId).toBeNull()
    expect(calls.some(({ path }) => path.startsWith("/api/containers"))).toBe(false)
    expect(putToOss).toHaveBeenCalledOnce()
    expect(result.current.attachments.assetIds()).toEqual(["asset-1"])
  })

  it("never uploads private bytes to the shared sandbox when OSS returns 503", async () => {
    createReply = async () => json({ detail: "OSS unavailable" }, 503)
    const { result } = hook()
    await waitFor(() => expect(result.current.canAttach).toBe(true))
    act(() => result.current.attachments.addFiles([file()]))
    await waitFor(() => expect(result.current.attachments.items[0]?.status).toBe("error"))
    expect(uploadCalls().map(({ path }) => path)).toEqual(["/api/assets"])
    expect(JSON.parse(String(uploadCalls()[0].init?.body)).session_id).toBe(shared.id)
    expect(putToOss).not.toHaveBeenCalled()
  })

  it.each([undefined, { ...privateSession, id: "other-session" }, { ...privateSession, user_id: undefined }])(
    "does not start an upload until the matching Session owner is loaded (%s)", async (current) => {
      const { result, rerender } = renderHook(({ session }) => useComposerAttachments(false, shared.id, session), {
        initialProps: { session: current as Session | undefined }, wrapper,
      })
      act(() => result.current.attachments.addFiles([file()]))
      expect(result.current.canAttach).toBe(false)
      expect(result.current.attachments.items).toEqual([])
      expect(calls).toEqual([])
      rerender({ session: privateSession })
      act(() => result.current.attachments.addFiles([file()]))
      await waitFor(() => expect(result.current.attachments.items[0]?.status).toBe("done"))
      expect(JSON.parse(String(uploadCalls()[0].init?.body)).session_id).toBe(shared.id)
    },
  )

  it.each(["loading", "missing", "foreign"])("waits for the current actor (%s)", (state) => {
    if (state === "loading") useAuthStore.getState().setLoading(true)
    if (state === "missing") useAuthStore.getState().setAuth("test-token", null)
    if (state === "foreign") useAuthStore.getState().setAuth("test-token", { ...actor, id: "peer" })
    const { result } = hook()
    act(() => result.current.attachments.addFiles([file()]))
    expect(result.current.canAttach).toBe(false)
    expect(result.current.attachments.items).toEqual([])
    expect(calls).toEqual([])
  })

  it.each(["pending", "private", "unmount"])("does not follow a delayed shared 503 after becoming %s", async (transition) => {
    let reply!: (response: Response) => void
    createReply = () => new Promise<Response>((resolve) => { reply = resolve })
    const { result, rerender, unmount } = hook(shared)
    await waitFor(() => expect(result.current.canAttach).toBe(true))
    const retainedAdd = result.current.attachments.addFiles
    act(() => retainedAdd([file()]))
    await waitFor(() => expect(uploadCalls()).toHaveLength(1))
    if (transition === "unmount") unmount()
    else rerender({ current: transition === "private" ? privateSession : undefined })
    act(() => retainedAdd([file()]))
    await act(async () => { reply(json({ detail: "OSS unavailable" }, 503)) })
    expect(uploadCalls().map(({ path }) => path)).toEqual(["/api/assets"])
    expect(putToOss).not.toHaveBeenCalled()
  })
})

describe("ordinary shared uploads", () => {
  it.each(["existing", "new"])("keeps the %s ordinary composer sandbox fallback", async (kind) => {
    createReply = async () => json({ detail: "OSS unavailable" }, 503)
    const { result } = renderHook(() => useComposerAttachments(false, kind === "existing" ? shared.id : undefined, kind === "existing" ? shared : undefined), { wrapper })
    await waitFor(() => expect(result.current.canAttach).toBe(true))
    act(() => result.current.attachments.addFiles([file()]))
    await waitFor(() => expect(result.current.attachments.items[0]?.status).toBe("done"))
    expect(uploadCalls().map(({ path }) => path)).toEqual(["/api/assets", "/api/containers/shared-desktop/files/upload"])
    expect(JSON.parse(String(uploadCalls()[0].init?.body))).not.toHaveProperty("session_id")
    const form = uploadCalls()[1].init?.body as FormData
    expect(form.get("file")).toBeInstanceOf(File)
    expect(result.current.attachments.decorate("request")).toBe("request\n\n[attachments]\n- /workspace/uploads/private.txt")
  })
})
