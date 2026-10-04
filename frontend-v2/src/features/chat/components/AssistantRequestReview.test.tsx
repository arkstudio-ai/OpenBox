import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { afterEach, beforeEach, expect, it, vi } from "vitest"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import { http } from "@/shared/api/http"
import type { AssistantRequestBinding } from "@/shared/types/api"
import { AssistantRequestReview } from "./AssistantRequestReview"

vi.mock("react-i18next", () => ({ useTranslation: () => ({ t: (key: string) => key }) }))
const binding: AssistantRequestBinding = { kind: "permission", task_id: "task", assistant_session_id: "main",
  workspace_id: "workspace", project_id: "project", run_id: "run", generation: 1,
  request_revision: "revision", options_hash: "hash" }
const review = { segments: ["Operation", "Full target scope", "Permanent scope"],
  display_token: "signed-token", request_revision: binding.request_revision }
let observers: Observer[]
let client: QueryClient
class Observer {
  nodes: Element[] = []
  observe = (node: Element) => { this.nodes.push(node) }
  disconnect = vi.fn(() => { this.nodes = [] })
  constructor(readonly callback: IntersectionObserverCallback) { observers.push(this) }
  show(indices: number[], ratio = 1) {
    this.callback(indices.map((index) => ({ target: this.nodes[index], isIntersecting: true,
      intersectionRatio: ratio } as IntersectionObserverEntry)), this as unknown as IntersectionObserver)
  }
}
beforeEach(() => {
  observers = []
  vi.stubGlobal("IntersectionObserver", Observer)
  vi.spyOn(document, "visibilityState", "get").mockReturnValue("visible")
  useAuthStore.setState({ user: { id: "owner" } as never })
  useWorkspaceStore.setState({ currentId: "workspace" })
  client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  vi.spyOn(http, "get").mockResolvedValue(review)
  vi.spyOn(http, "post").mockResolvedValue({ state: "displayed" })
})
afterEach(() => { cleanup(); client.clear(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

function mount() {
  return render(<QueryClientProvider client={client}>
    <AssistantRequestReview requestId="request" binding={binding} />
  </QueryClientProvider>)
}
async function openReview() {
  const details = screen.getByText("assistant.requests.review").closest("details")!
  details.open = true
  fireEvent(details, new Event("toggle"))
  await screen.findByText("Full target scope")
  await waitFor(() => expect(observers).toHaveLength(1))
  return details
}

it("requires every block to be fully visible and records display without approving", async () => {
  mount()
  expect(http.get).not.toHaveBeenCalled()
  await openReview()
  expect(http.post).not.toHaveBeenCalled()
  act(() => observers[0].show([0, 1, 2], 0.5))
  expect(http.post).not.toHaveBeenCalled()
  act(() => observers[0].show([0, 1]))
  expect(http.post).not.toHaveBeenCalled()
  act(() => { observers[0].show([2]); observers[0].show([0, 1, 2]) })
  await waitFor(() => expect(http.post).toHaveBeenCalledExactlyOnceWith("/api/assistant/requests/displayed",
    { display_token: review.display_token }, expect.any(Object)))
  expect(await screen.findByText("assistant.requests.reviewed")).toBeTruthy()
})

it("does not count background rendering and measures again after foregrounding", async () => {
  mount()
  await openReview()
  vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden")
  act(() => observers[0].show([0, 1, 2]))
  expect(http.post).not.toHaveBeenCalled()
  vi.spyOn(document, "visibilityState", "get").mockReturnValue("visible")
  act(() => document.dispatchEvent(new Event("visibilitychange")))
  expect(http.post).not.toHaveBeenCalled()
  act(() => observers[0].show([0, 1, 2]))
  await waitFor(() => expect(http.post).toHaveBeenCalledOnce())
})

it("rejects queued observations after the review closes", async () => {
  mount()
  const details = await openReview()
  const observer = observers[0]
  details.open = false
  fireEvent(details, new Event("toggle"))
  await waitFor(() => expect(observer.nodes).toHaveLength(0))
  act(() => observer.show([0, 1, 2]))
  expect(http.post).not.toHaveBeenCalled()
})

it("cannot submit display evidence after switching accounts", async () => {
  mount()
  await openReview()
  act(() => {
    useAuthStore.setState({ user: { id: "other" } as never })
    observers[0].show([0, 1, 2])
  })
  await act(async () => {})
  expect(http.post).not.toHaveBeenCalled()
})

it("does not observe a response for an obsolete request revision", async () => {
  vi.mocked(http.get).mockResolvedValue({ ...review, request_revision: "old" })
  mount()
  const details = screen.getByText("assistant.requests.review").closest("details")!
  details.open = true
  fireEvent(details, new Event("toggle"))
  await screen.findByText("Full target scope")
  expect(observers).toHaveLength(0)
  expect(http.post).not.toHaveBeenCalled()
})
