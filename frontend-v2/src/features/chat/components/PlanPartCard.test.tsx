import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, expect, it, vi } from "vitest"
import type { PlanPart } from "@/shared/types/api"
import { PlanPartCard } from "./PlanPartCard"

const actions = vi.hoisted(() => ({ accept: vi.fn(), reject: vi.fn(), save: vi.fn() }))
vi.mock("../api/plan", () => ({
  usePlanDecision: () => ({
    accept: { mutate: actions.accept, isPending: false },
    reject: { mutate: actions.reject, isPending: false },
  }),
  useSavePlan: () => ({ mutate: actions.save, isPending: false }),
}))
vi.mock("react-i18next", () => ({ useTranslation: () => ({ t: (key: string) => key }) }))
vi.mock("./Markdown", () => ({ default: ({ text }: { text: string }) => <p>{text}</p> }))

const plan: PlanPart = { id: "plan", type: "plan", path: "/workspace/plan.md", status: "ready",
  content: "Implement exactly this version." }
afterEach(() => { cleanup(); vi.clearAllMocks() })

it("shows the managed plan without legacy edit or approval actions", async () => {
  render(<PlanPartCard part={{ ...plan, review_via_question: true }} sessionId="session" />)
  expect(await screen.findByText(plan.content)).toBeTruthy()
  expect(screen.queryByRole("button", { name: "plan.review.edit" })).toBeNull()
  expect(screen.queryByRole("button", { name: "plan.review.accept" })).toBeNull()
  expect(screen.queryByRole("button", { name: "plan.review.reject" })).toBeNull()
  expect(actions.accept).not.toHaveBeenCalled()
})

it("retains ordinary plan decisions and stops editing when the card becomes immutable", () => {
  const view = render(<PlanPartCard part={plan} sessionId="session" />)
  fireEvent.click(screen.getByRole("button", { name: "plan.review.accept" }))
  expect(actions.accept).toHaveBeenCalledOnce()
  fireEvent.click(screen.getByRole("button", { name: "plan.review.edit" }))
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "Unapproved replacement" } })
  view.rerender(<PlanPartCard part={{ ...plan, review_via_question: true }} sessionId="session" />)
  expect(screen.queryByRole("textbox")).toBeNull()
  expect(screen.queryByRole("button", { name: "plan.review.save" })).toBeNull()
  expect(actions.save).not.toHaveBeenCalled()
})
