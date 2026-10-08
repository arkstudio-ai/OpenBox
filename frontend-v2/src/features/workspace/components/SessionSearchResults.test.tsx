import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { cleanup, render, screen, within } from "@testing-library/react"
import { MemoryRouter } from "react-router"
import { afterEach, describe, expect, it, vi } from "vitest"
import { http } from "@/shared/api/http"
import type { Project, Session } from "@/shared/types/api"
import type { SessionSearchHit } from "../api/sessions"
import { splitMatches } from "../lib/splitMatches"
import { SessionSearchResults } from "./SessionSearchResults"

vi.mock("react-i18next", async (original) => ({
  ...await original<typeof import("react-i18next")>(),
  useTranslation: () => ({ t: (key: string) => key }),
}))

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

const projects = [{ id: "p1", name: "贪吃蛇" }, { id: "p2", name: "运营" }] as Project[]
const sessions = [
  { id: "c1", title: "贪吃蛇界面讨论", project_id: "p1", kind: "normal", updated_at: "2026-10-08T02:00:00Z" },
  { id: "c2", title: "周计划", project_id: "p2", kind: "normal", updated_at: "2026-10-08T03:00:00Z" },
  { id: "c3", title: "贪吃蛇日报", project_id: "p1", kind: "cron", updated_at: "2026-10-08T04:00:00Z" },
] as Session[]
const hit = (over: Partial<SessionSearchHit>): SessionSearchHit => ({
  session_id: "c1", title: "贪吃蛇界面讨论", project_id: "p1", kind: "normal", match: "title", snippet: "",
  role: null, time: "2026-10-08T02:00:00Z", ...over,
})

function mount(query: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <SessionSearchResults query={query} sessions={sessions} projects={projects} />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe("SessionSearchResults", () => {
  it("lists title matches at once, then adds conversations whose messages match", async () => {
    const get = vi.spyOn(http, "get").mockResolvedValue([
      hit({ snippet: "贪吃蛇的配色换成暗色", role: "user" }),
      hit({ session_id: "c2", title: "周计划", project_id: "p2", match: "content", role: "assistant",
            snippet: "…下周开始优化贪吃蛇的计分逻辑…" }),
    ])
    mount(" 贪吃蛇 ")
    const results = within(screen.getByRole("list", { name: "searchResults" }))
    // Before the server answers: the loaded titles (not the scheduled run's), and a note that messages are coming.
    expect(results.getAllByRole("link").map((link) => link.textContent)).toEqual(["贪吃蛇界面讨论贪吃蛇"])
    expect(results.getByText("searching")).toBeTruthy()

    const plan = await results.findByRole("link", { name: /周计划/ })
    expect(get).toHaveBeenCalledWith(`/api/agent/session/search?${new URLSearchParams({ q: "贪吃蛇" })}`)
    expect(plan.getAttribute("href")).toBe("/app/s/c2")
    expect(plan.textContent).toBe("周计划运营 · …下周开始优化贪吃蛇的计分逻辑…")
    expect([...plan.querySelectorAll("mark")].map((mark) => mark.textContent)).toEqual(["贪吃蛇"])
    expect(results.getByRole("link", { name: /贪吃蛇界面讨论/ }).textContent).toContain("searchYou贪吃蛇的配色换成暗色")
    expect(results.queryByText("searching")).toBeNull()
  })

  it("opens the personal assistant's conversation on its page and says when nothing matches", async () => {
    vi.spyOn(http, "get").mockResolvedValueOnce([
      hit({ session_id: "a1", title: "Assistant", project_id: "p0", kind: "assistant", match: "content",
            snippet: "提醒我周五交房租", role: "user" }),
    ])
    mount("房租")
    const assistant = await screen.findByRole("link", { name: /assistant/ })
    expect(assistant.getAttribute("href")).toBe("/app/assistant")
    expect(assistant.textContent).toBe("common:assistantName.titlesearchYou提醒我周五交房租")
    cleanup()

    vi.spyOn(http, "get").mockResolvedValueOnce([])
    mount("没有的词")
    expect(await screen.findByText("searchEmpty")).toBeTruthy()
  })

  it("keeps the title matches when the message search fails", async () => {
    vi.spyOn(http, "get").mockRejectedValue(new Error("offline"))
    mount("周计划")
    expect(await screen.findByText("searchFailed")).toBeTruthy()
    expect(screen.getByRole("link", { name: /周计划/ }).getAttribute("href")).toBe("/app/s/c2")
  })
})

describe("splitMatches", () => {
  it("cuts at every match, ignoring case", () => {
    expect(splitMatches("Hello hello HELLO!", "hello")).toEqual([
      { text: "Hello", match: true }, { text: " ", match: false }, { text: "hello", match: true },
      { text: " ", match: false }, { text: "HELLO", match: true }, { text: "!", match: false },
    ])
    expect(splitMatches("贪吃蛇", "")).toEqual([{ text: "贪吃蛇", match: false }])
  })
})
