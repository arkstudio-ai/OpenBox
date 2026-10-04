import type { ReactNode } from "react"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { act, cleanup, renderHook } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import type { MessageWithParts } from "@/shared/types/api"
import { AssistantReadContext } from "./assistant-read-context"
import type { AssistantSnapshot } from "../api/assistant"
import { assistantKeys } from "../api/assistant"
import { readAssistantMessages, type TranscriptPage } from "../api/assistant-transcript"
import { useVerifiedAssistantCopy } from "./useVerifiedAssistantCopy"

const copy = vi.hoisted(() => vi.fn())
vi.mock("@/shared/hooks/useCopy", () => ({ useCopy: () => ({ copy, copied: false }) }))
vi.mock("@/shared/hooks/useApiErrorMessage", () => ({ useApiErrorMessage: () => () => "Read failed" }))
vi.mock("@/shared/ui/Toast", () => ({ toast: vi.fn() }))
vi.mock("react-i18next", () => ({ useTranslation: () => ({ t: (key: string) => key }) }))
vi.mock("../api/assistant-transcript", async (original) => ({
  ...await original<typeof import("../api/assistant-transcript")>(), readAssistantMessages: vi.fn(),
}))
const answer: MessageWithParts = { id: "answer", session_id: "s", role: "assistant", created_at: "", finish: "stop",
  source_status: "available", source_checked_at: "2026-10-03T10:00:01.000000+00:00",
  parts: [{ id: "p", type: "text", channel: "final", text: "Fresh verified answer" }] }
beforeEach(() => {
  vi.clearAllMocks()
  useAuthStore.setState({ user: { id: "actor", username: "actor", role: "user" } })
  useWorkspaceStore.setState({ currentId: "workspace" })
})
afterEach(cleanup)
function mount(privateMain = true, mode: "answer" | "text" | "fragment" = "answer", content = "Stale cached answer", execution = false) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const key = assistantKeys.transcript("actor", "workspace", "s", ["answer"])
  client.setQueryData(key, { messages: [answer] })
  const context = { snapshot: execution ? undefined : { answers: [] } as unknown as AssistantSnapshot, displayed: vi.fn() }
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>
    <AssistantReadContext.Provider value={privateMain ? context : null}>{children}</AssistantReadContext.Provider>
  </QueryClientProvider>
  return { ...renderHook(() => useVerifiedAssistantCopy("s", "answer", content, mode), { wrapper }), client, key }
}

describe("assistant clipboard authorization", () => {
  it("copies fresh authorized text instead of the content captured by the button", async () => {
    vi.mocked(readAssistantMessages).mockResolvedValue({ messages: [answer] })
    const { result } = mount()
    await act(() => result.current.copyReply())
    expect(readAssistantMessages).toHaveBeenCalledWith("s", ["answer"], "workspace")
    expect(copy).toHaveBeenCalledExactlyOnceWith("Fresh verified answer")
  })
  it("hides a source revoked between display and copy and leaves the clipboard untouched", async () => {
    const unavailable = { ...answer, source_status: "unavailable" as const, parts: [], source_checked_at: "2026-10-03T10:00:02.000000+00:00" }
    vi.mocked(readAssistantMessages).mockResolvedValue({ messages: [unavailable] })
    const { result, client, key } = mount()
    await act(() => result.current.copyReply())
    expect(copy).not.toHaveBeenCalled()
    expect(client.getQueryData<TranscriptPage>(key)?.messages).toEqual([unavailable])
  })
  it("does not copy after a workspace switch while verification was in flight", async () => {
    let finish!: (page: TranscriptPage) => void
    vi.mocked(readAssistantMessages).mockReturnValue(new Promise((resolve) => { finish = resolve }))
    const { result } = mount()
    await act(async () => {
      const pending = result.current.copyReply()
      useWorkspaceStore.setState({ currentId: "other-workspace" })
      finish({ messages: [answer] })
      await pending
    })
    expect(copy).not.toHaveBeenCalled()
  })
  it("preserves ordinary chat copy without an assistant read", async () => {
    const { result } = mount(false)
    await act(() => result.current.copyReply())
    expect(copy).toHaveBeenCalledExactlyOnceWith("Stale cached answer")
    expect(readAssistantMessages).not.toHaveBeenCalled()
  })
  it("revalidates execution user input and code fragments without a main snapshot", async () => {
    vi.mocked(readAssistantMessages).mockResolvedValue({ messages: [{...answer, role:"user"}] })
    const user = mount(true, "text", "old user text", true)
    await act(() => user.result.current.copyReply())
    expect(copy).toHaveBeenLastCalledWith("Fresh verified answer")
    const code = mount(true, "fragment", "verified", true)
    await act(() => code.result.current.copyReply())
    expect(copy).toHaveBeenLastCalledWith("verified")
    const staleCode = mount(true, "fragment", "removed code", true)
    copy.mockClear()
    await act(() => staleCode.result.current.copyReply())
    expect(copy).not.toHaveBeenCalled()
  })
  it("refuses a late available copy response after a newer poll revoked its source", async () => {
    let finish!: (page: TranscriptPage) => void
    vi.mocked(readAssistantMessages).mockReturnValue(new Promise((resolve) => { finish = resolve }))
    const { result, client, key } = mount()
    await act(async () => {
      const copying = result.current.copyReply()
      client.setQueryData(key, { messages: [{...answer, parts:[], source_status:"unavailable", source_checked_at:"2026-10-03T10:00:02Z"}] })
      finish({ messages:[answer] })
      await copying
    })
    expect(copy).not.toHaveBeenCalled()
  })
  it("does not copy after the message view has unmounted", async () => {
    let finish!: (page: TranscriptPage) => void
    vi.mocked(readAssistantMessages).mockReturnValue(new Promise((resolve) => { finish = resolve }))
    const view = mount()
    const copying = view.result.current.copyReply()
    view.unmount()
    finish({messages:[answer]})
    await copying
    expect(copy).not.toHaveBeenCalled()
  })
})
