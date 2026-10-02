import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { cleanup, render, screen } from "@testing-library/react"
import { MemoryRouter } from "react-router"
import { afterEach, expect, it, vi } from "vitest"
import { http } from "@/shared/api/http"
import type { MemoryRecord } from "@/shared/api/memory"
import { MemoryGroups } from "./MemoryGroups"

let client: QueryClient
afterEach(() => {
  cleanup()
  client?.clear()
  vi.restoreAllMocks()
})
it("groups related facts once and preserves unmatched facts", async () => {
  vi.spyOn(http, "get").mockResolvedValue({
    groups: [
      { id: "g1", title: "工作约定", page_id: "wiki1", memory_ids: ["1", "2"] },
      { id: "g2", title: "近义标题", page_id: "wiki2", memory_ids: ["2"] },
      { id: "g3", title: "单条主题", page_id: "wiki3", memory_ids: ["3"] },
    ],
  })
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <MemoryGroups
          active
          projectId="p1"
          memories={
            [
              { id: "1", summary: "A" },
              { id: "2", summary: "B" },
              { id: "3", summary: "C" },
            ] as MemoryRecord[]
          }
        >
          {(memory) => <p key={memory.id}>{memory.summary}</p>}
        </MemoryGroups>
      </MemoryRouter>
    </QueryClientProvider>,
  )
  await screen.findByRole("link", { name: "工作约定" })
  expect(screen.getAllByText("B")).toHaveLength(1)
  expect(screen.getByText("C")).toBeTruthy()
  expect(screen.queryByText("近义标题")).toBeNull()
  expect(screen.queryByText("单条主题")).toBeNull()
})
