import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { ApiError, http } from "@/shared/api/http"
import { toast } from "@/shared/ui/Toast"
import type { MessagePart, ToolPart } from "@/shared/types/api"
import { AssistantMemoryReceipts } from "./AssistantMemoryReceipts"

vi.mock("@/shared/api/http", async (original) => ({
  ...await original<typeof import("@/shared/api/http")>(), http: { post: vi.fn() },
}))
vi.mock("react-i18next", () => ({ useTranslation: () => ({
  t: (key: string, values?: { summary?: string }) => values?.summary ? `${key}|${values.summary}` : key,
}) }))
vi.mock("@/shared/hooks/useApiErrorMessage", () => ({ useApiErrorMessage: () => () => "Request failed" }))
vi.mock("@/shared/ui/Toast", () => ({ toast: vi.fn() }))

const call = (id: string, tool: string, output: unknown): ToolPart =>
  ({ type: "tool", id, tool, status: "completed", output: JSON.stringify(output) })
const remembered = call("remember-part", "memory.remember",
  { state: "remembered", memory_id: "memory-1", summary: "以后回复都用表格", revision: 2, scope: "personal" })
beforeEach(() => { vi.clearAllMocks() })
afterEach(cleanup)
function mount(parts: MessagePart[]) {
  return render(<AssistantMemoryReceipts parts={parts} />)
}

describe("memory chips under an answer", () => {
  it("shows one compact chip per memory change, in call order", () => {
    mount([
      remembered,
      call("again", "memory.remember", { state: "already_remembered", memory_id: "memory-0" }),
      call("refused", "memory.remember", { state: "refused", reason: "credential" }),
      call("paused", "memory.remember", { state: "paused" }),
      call("updated", "memory.update", { state: "updated", memory_id: "memory-2", summary: "回复用中文表格", revision: 4 }),
      call("forgot", "memory.forget", { state: "forgotten", memory_id: "memory-3", note: "…" }),
      { type: "text", id: "answer", text: "好的" },
    ])
    const group = screen.getByRole("group", { name: "assistant.memory.label" })
    expect([...group.children].map((chip) => chip.textContent)).toEqual([
      "assistant.memory.remembered|以后回复都用表格·assistant.memory.undo",
      "assistant.memory.alreadyRemembered",
      "assistant.memory.refused",
      "assistant.memory.paused",
      "assistant.memory.updated|回复用中文表格",
      "assistant.memory.forgotten",
    ])
    // Only a fresh memory offers an undo.
    expect(screen.getAllByRole("button")).toHaveLength(1)
  })

  it("renders nothing for a turn without memory changes", () => {
    const view = mount([call("search", "memory.search", { items: [] }), { type: "text", id: "answer", text: "好的" }])
    expect(view.container.textContent).toBe("")
  })

  it("undoes a fresh memory through the forget API and then shows it as undone", async () => {
    let finish!: (value: unknown) => void
    vi.mocked(http.post).mockReturnValue(new Promise((resolve) => { finish = resolve }))
    mount([remembered])
    fireEvent.click(screen.getByRole("button", { name: "assistant.memory.undo" }))
    expect(http.post).toHaveBeenCalledExactlyOnceWith("/api/memories/memory-1/forget", {
      expected_revision: 2, request_id: "assistant-undo:remember-part", mode: "memory", source_ids: [] })
    const pending = screen.getByRole("button", { name: "assistant.memory.undoing" }) as HTMLButtonElement
    expect(pending.disabled).toBe(true)
    await act(async () => finish({ ok: true }))
    const undone = screen.getByRole("button", { name: "assistant.memory.undone" }) as HTMLButtonElement
    expect(undone.disabled).toBe(true)
    fireEvent.click(undone)
    expect(http.post).toHaveBeenCalledTimes(1)
  })

  it("keeps the memory and its undo after a failed request", async () => {
    vi.mocked(http.post).mockRejectedValueOnce(new TypeError("offline"))
    mount([remembered])
    fireEvent.click(screen.getByRole("button", { name: "assistant.memory.undo" }))
    await waitFor(() => expect(toast).toHaveBeenCalledWith("error", "Request failed"))
    expect((screen.getByRole("button", { name: "assistant.memory.undo" }) as HTMLButtonElement).disabled).toBe(false)
  })

  it("explains a memory that changed after it was remembered", async () => {
    vi.mocked(http.post).mockRejectedValueOnce(new ApiError(409, "MEMORY_REVISION_CONFLICT", "Memory changed"))
    mount([remembered])
    fireEvent.click(screen.getByRole("button", { name: "assistant.memory.undo" }))
    await waitFor(() => expect(toast).toHaveBeenCalledWith("error", "assistant.memory.undoChanged"))
  })

  it("undoes a reloaded receipt without a revision, letting the server skip that check", async () => {
    vi.mocked(http.post).mockResolvedValue({ ok: true })
    mount([{ type: "tool", id: "reloaded", tool: "memory.remember", status: "completed",
      metadata: { assistant_memory: { state: "remembered", memory_id: "memory-1", summary: "以后回复都用表格" } } }])
    fireEvent.click(screen.getByRole("button", { name: "assistant.memory.undo" }))
    await screen.findByRole("button", { name: "assistant.memory.undone" })
    const [, body] = vi.mocked(http.post).mock.calls[0]
    expect(JSON.parse(JSON.stringify(body))).toEqual({ request_id: "assistant-undo:reloaded", mode: "memory", source_ids: [] })
  })
})
