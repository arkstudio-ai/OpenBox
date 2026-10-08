import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react"
import type { ReactNode } from "react"
import { MemoryRouter } from "react-router"
import { afterEach, beforeEach, expect, it, vi } from "vitest"
import { http } from "@/shared/api/http"
import { toast } from "@/shared/ui/Toast"
import { AssistantMeta } from "./AssistantMeta"

vi.mock("react-i18next", async (original) => ({
  ...(await original<typeof import("react-i18next")>()),
  useTranslation: () => ({ t: (key: string) => key, i18n: { language: "zh-CN" } }),
}))
vi.mock("@/shared/ui/Toast", () => ({ toast: vi.fn() }))

function wrap(node: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return (
    <QueryClientProvider client={client}>
      <MemoryRouter>{node}</MemoryRouter>
    </QueryClientProvider>
  )
}

const meta = {
  sessionId: "s1",
  messageId: "a1",
  content: "回答",
  createdAt: "2026-10-08T10:00:00Z",
  streaming: false,
  durationSec: 3,
}
let post: ReturnType<typeof vi.spyOn>

beforeEach(() => {
  vi.spyOn(http, "get").mockResolvedValue({ id: "s1", kind: "assistant", status: "idle" })
  post = vi.spyOn(http, "post").mockResolvedValue({ ok: true })
})
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.mocked(toast).mockClear()
})

it("asks the personal assistant's user why after a thumbs-down, and sends the reason picked", async () => {
  const { rerender } = render(wrap(<AssistantMeta {...meta} minimal />))
  fireEvent.click(screen.getByRole("button", { name: "meta.dislikeReply" }))
  await waitFor(() =>
    expect(post).toHaveBeenCalledWith("/api/agent/session/s1/message/a1/reaction", { reaction: "down" }),
  )
  rerender(wrap(<AssistantMeta {...meta} minimal reaction="down" />)) // the store now holds the down
  const reasons = screen.getByRole("group", { name: "meta.reason.title" })
  fireEvent.click(within(reasons).getByRole("button", { name: "meta.reason.too_long" }))
  await waitFor(() =>
    expect(post).toHaveBeenLastCalledWith("/api/agent/session/s1/message/a1/reaction", {
      reaction: "down",
      reason: "too_long",
    }),
  )
  expect(
    within(reasons).getByRole("button", { name: "meta.reason.too_long" }).getAttribute("aria-pressed"),
  ).toBe("true")
  expect(toast).toHaveBeenCalledWith("success", "meta.reason.thanks")
})

it("does not ask in a work chat, where reasons are not used", async () => {
  const { rerender } = render(wrap(<AssistantMeta {...meta} />))
  fireEvent.click(screen.getByRole("button", { name: "meta.dislikeReply" }))
  rerender(wrap(<AssistantMeta {...meta} reaction="down" />))
  await waitFor(() => expect(post).toHaveBeenCalled())
  expect(screen.queryByRole("group", { name: "meta.reason.title" })).toBeNull()
})
