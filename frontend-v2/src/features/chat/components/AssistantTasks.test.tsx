import { cleanup, fireEvent, render, screen, within } from "@testing-library/react"
import { MemoryRouter } from "react-router"
import { afterEach, beforeEach, expect, it, vi } from "vitest"
import type { AssistantWatchItem } from "../api/assistant-watch"
import { AssistantTopbarActions } from "./AssistantTasks"

const api = vi.hoisted(() => ({ watch: vi.fn() }))
vi.mock("../api/assistant-watch", () => ({ useAssistantWatch: api.watch }))
vi.mock("./AssistantTaskCard", () => ({ AssistantTaskCard: ({ taskId }: { taskId: string }) => <p>card:{taskId}</p> }))
vi.mock("./AssistantLinkExisting", () => ({ AssistantLinkExisting: () => <p>link list</p> }))
vi.mock("react-i18next", () => ({ useTranslation: () => ({ t: (key: string, options?: { count?: number }) =>
  options?.count !== undefined ? `${key}:${options.count}` : key }) }))
vi.mock("@/shared/hooks/useApiErrorMessage", () => ({ useApiErrorMessage: () => () => "Unavailable" }))

const base: AssistantWatchItem = { task_id: "t", title: "Task", project: { id: "p", name: "Project" }, session_id: "s",
  session_status: "idle", desired_state: "running", observed_state: "idle", revision: 1, updated_at: "now", pending_questions: 0 }
const item = (id: string, patch: Partial<AssistantWatchItem>) => ({ ...base, task_id: id, ...patch })

beforeEach(() => api.watch.mockReturnValue({ data: { items: [
  item("asking", { pending_questions: 2 }),
  item("working", { session_status: "busy" }),
  item("paused", { desired_state: "paused", observed_state: "paused" }),
  item("finished", { latest_result: { result_id: "r", outcome: "succeeded", delivery_state: "processed", created_at: "now", summary: "ok" } }),
], has_more: true }, isPending: false }))
afterEach(cleanup)

function mount() {
  render(<MemoryRouter><AssistantTopbarActions /></MemoryRouter>)
}

it("counts unfinished work on the button and flags work waiting on the user", () => {
  mount()
  const button = screen.getByRole("button", { name: "assistant.taskList.buttonWaiting:1" })
  expect(button.textContent).toContain("3")
})

it("opens a drawer grouping what needs the user first, then unfinished, then finished", () => {
  mount()
  fireEvent.click(screen.getByRole("button", { name: /assistant.taskList.button/ }))
  const drawer = screen.getByRole("dialog")
  const groups = within(drawer).getAllByRole("region").map((group) => group.getAttribute("aria-label"))
  expect(groups).toEqual(["assistant.taskList.groups.waiting", "assistant.taskList.groups.active", "assistant.taskList.groups.finished"])
  expect(within(drawer).getAllByText(/^card:/).map((node) => node.textContent)).toEqual(
    ["card:asking", "card:working", "card:paused", "card:finished"])
  expect(within(drawer).getByText("assistant.taskList.more")).toBeTruthy()
})

it("says what to do when nothing has been handed over yet", () => {
  api.watch.mockReturnValue({ data: { items: [], has_more: false }, isPending: false })
  mount()
  fireEvent.click(screen.getByRole("button", { name: "assistant.taskList.button" }))
  expect(screen.getByText("assistant.taskList.emptyTitle")).toBeTruthy()
})

it("lets the user hand an existing conversation over from the drawer's foot", () => {
  mount()
  fireEvent.click(screen.getByRole("button", { name: /assistant.taskList.button/ }))
  fireEvent.click(screen.getByRole("button", { name: "assistant.taskList.followExisting" }))
  expect(screen.getByText("link list")).toBeTruthy()
})

it("closes on Escape", () => {
  mount()
  fireEvent.click(screen.getByRole("button", { name: /assistant.taskList.button/ }))
  fireEvent.keyDown(window, { key: "Escape" })
  expect(screen.queryByRole("dialog")).toBeNull()
})
