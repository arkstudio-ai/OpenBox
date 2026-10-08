import { cleanup, render, screen, within } from "@testing-library/react"
import { MemoryRouter } from "react-router"
import { afterEach, expect, it, vi } from "vitest"
import type { MemoryRecord } from "@/shared/api/memory"
import { MemoryList } from "./MemoryList"

vi.mock("react-i18next", async (original) => ({
  ...(await original<typeof import("react-i18next")>()),
  useTranslation: () => ({
    t: (key: string, opts?: Record<string, unknown>) => (opts?.date ? `${key}:${String(opts.date)}` : key),
  }),
}))
afterEach(cleanup)

const hoursAgo = (hours: number) => new Date(Date.now() - hours * 3_600_000).toISOString()
const memory = (
  id: string,
  summary: string,
  updated: string,
  extra: Partial<MemoryRecord> = {},
): MemoryRecord => ({
  id,
  summary,
  type: "USER_PROFILE",
  scope: "LONG_TERM",
  status: "ACTIVE",
  revision: 1,
  updated_at: updated,
  ...extra,
})

function list(memories: MemoryRecord[], timeline: boolean) {
  return render(
    <MemoryRouter>
      <MemoryList
        memories={memories}
        timeline={timeline}
        query=""
        projectId=""
        busy={false}
        topicsOf={new Map()}
        scopeName={() => ""}
        onOpen={vi.fn()}
        onEdit={vi.fn()}
        onForget={vi.fn()}
      />
    </MemoryRouter>,
  )
}

it("reads as a timeline of what was learned when, and says how each came to be", () => {
  const now = new Date()
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime()
  const lastNight = new Date(startOfToday - 3_600_000).toISOString()
  list(
    [
      memory("a", "用户嫌回答太长，希望先说结论", hoursAgo(0), {
        owner: "SYSTEM_VERIFIED",
        fact_key: "personal.style.length",
      }),
      memory("b", "用户这周五要加班", hoursAgo(0), {
        owner: "SYSTEM_VERIFIED",
        expires_at: "2026-10-09T16:00:00Z",
      }),
      memory("c", "用户周末一般会去游泳", lastNight, { owner: "USER_CONFIRMED" }),
      memory("d", "用户对花生过敏", hoursAgo(24 * 30), { owner: "USER_CONFIRMED" }),
    ],
    true,
  )
  const today = screen.getByRole("region", { name: "timeline.today" })
  expect(within(today).getAllByRole("listitem")).toHaveLength(2)
  expect(within(today).getAllByText("how.learned")).toHaveLength(2)
  expect(within(today).getByText("how.style")).toBeTruthy()
  // A plan lasts to the end of its last day: the expiry at the next midnight reads as that day.
  expect(within(today).getByText(/^how\.until:/).textContent).not.toContain(":undefined")
  expect(
    within(screen.getByRole("region", { name: "timeline.yesterday" })).getByText("how.added"),
  ).toBeTruthy()
  expect(
    within(screen.getByRole("region", { name: "timeline.earlier" })).getByText("用户对花生过敏"),
  ).toBeTruthy()
})

it("stays one list when searching", () => {
  list([memory("a", "用户爱吃辣", hoursAgo(1))], false)
  expect(screen.queryByRole("region")).toBeNull()
  expect(screen.getAllByRole("listitem")).toHaveLength(1)
})
