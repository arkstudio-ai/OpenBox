import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import type { ReactNode } from "react"
import { MemoryRouter } from "react-router"
import { afterEach, expect, it, vi } from "vitest"
import { http } from "@/shared/api/http"
import { RecalledMemories } from "./RecalledMemories"

vi.mock("react-i18next", async (original) => ({
  ...(await original<typeof import("react-i18next")>()),
  useTranslation: () => ({
    t: (key: string, opts?: Record<string, unknown>) =>
      opts?.count !== undefined ? `${key}:${opts.count}` : key,
  }),
}))
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

function wrap(node: ReactNode, client = new QueryClient({ defaultOptions: { queries: { retry: false } } })) {
  return (
    <QueryClientProvider client={client}>
      <MemoryRouter>{node}</MemoryRouter>
    </QueryClientProvider>
  )
}

it("says how many memories the reply drew on and lists them on demand, with where to fix them", async () => {
  const get = vi.spyOn(http, "get").mockResolvedValue({
    recalls: {
      "msg-1": [
        { id: "m1", summary: "用户周末一般会去游泳", type: "USER_PROFILE", project_id: null },
        { id: "m2", summary: "用户对花生过敏", type: "CONSTRAINT", project_id: null },
      ],
    },
  })
  render(wrap(<RecalledMemories sessionId="s1" messageId="msg-1" streaming={false} />))
  const chip = await screen.findByRole("button", { name: "assistant.recalled.label:2" })
  expect(get).toHaveBeenCalledWith("/api/memories/recalled/s1")
  expect(screen.queryByText("用户对花生过敏")).toBeNull()
  fireEvent.click(chip)
  expect(chip.getAttribute("aria-expanded")).toBe("true")
  expect(screen.getByText("用户周末一般会去游泳")).toBeTruthy()
  expect(screen.getByRole("link", { name: "assistant.recalled.manage" }).getAttribute("href")).toBe(
    "/app/wiki?view=memories",
  )
})

it("shows nothing while answering, for a reply that drew on nothing, and reads again when a reply ends", async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const get = vi.spyOn(http, "get").mockResolvedValue({ recalls: {} })
  const { rerender } = render(wrap(<RecalledMemories sessionId="s1" messageId="msg-2" streaming />, client))
  await waitFor(() => expect(get).toHaveBeenCalledTimes(1))
  expect(screen.queryByRole("button")).toBeNull()
  get.mockResolvedValue({
    recalls: { "msg-2": [{ id: "m1", summary: "用户爱吃辣", type: null, project_id: null }] },
  })
  rerender(wrap(<RecalledMemories sessionId="s1" messageId="msg-2" streaming={false} />, client))
  expect(await screen.findByRole("button", { name: "assistant.recalled.label:1" })).toBeTruthy()
  expect(get).toHaveBeenCalledTimes(2) // read once more when the reply ended, not on a timer
})
