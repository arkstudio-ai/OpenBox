import { cleanup, render, screen } from "@testing-library/react"
import { afterEach, expect, it, vi } from "vitest"
import { InlineErrorCard } from "./InlineErrorCard"

const preserve = vi.hoisted(() => vi.fn(() => true))
vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}))
vi.mock("@/shared/hooks/useApiErrorMessage", () => ({
  useApiErrorMessage: () => String,
}))
vi.mock("../../api/message-actions", () => ({
  usePreserveAssistantEvidence: preserve,
  useRegenerate: () => ({ mutate: vi.fn(), isPending: false }),
  useDismissFailedTurn: () => ({ mutate: vi.fn(), isPending: false }),
}))
vi.mock("../../stores/model-choice", () => ({
  usePickedModel: () => undefined,
}))
afterEach(() => { cleanup(); preserve.mockReturnValue(true) })

it("keeps the disconnected desktop cause visible with the original request recovery guidance", () => {
  const message = "云桌面尚未连接，附件暂时无法传入。原请求和参考附件已保留；恢复后继续原任务。"
  const { container } = render(<InlineErrorCard sessionId="task" messageId="reply"
    streaming={false} error={{ code: "SANDBOX_NOT_READY", message, retryable: true }} />)
  expect(screen.getByText(message)).toBeTruthy()
  expect(container.querySelector("details")).toBeNull()
  expect(screen.queryByText("assistant.continueAfterError")).toBeNull()
})

it("shows committed submission facts without a collapsed detail or advice to resend", () => {
  const receipt = "个人助理本轮回复超时。御尚喜宴：指令已接收，正在等待原任务中的确认。"
  const { container } = render(<InlineErrorCard sessionId="main" messageId="reply"
    streaming={false} error={{ code: "ASSISTANT_TURN_BUDGET", message: receipt }} />)
  expect(screen.getByText("assistant.replyIncomplete")).toBeTruthy()
  expect(screen.getByText(receipt)).toBeTruthy()
  expect(container.querySelector("details")).toBeNull()
  expect(screen.queryByText("assistant.continueAfterError")).toBeNull()
  expect(screen.queryByRole("button")).toBeNull()
})

it("preserves ordinary assistant error guidance", () => {
  const { container } = render(<InlineErrorCard sessionId="main" messageId="reply"
    streaming={false} error={{ code: "OTHER", message: "other failure" }} />)
  expect(screen.getByText("meta.errorTitle")).toBeTruthy()
  expect(screen.getByText("assistant.continueAfterError")).toBeTruthy()
  expect(container.querySelector("details")).toBeTruthy()
})

it("shows the subscription cause without suggesting another message or collapsing it", () => {
  const message = "当前没有有效的付费套餐，请续订后继续原任务。参考图已保留。"
  const { container } = render(<InlineErrorCard sessionId="main" messageId="reply"
    streaming={false} error={{ code: "SANDBOX_SUBSCRIPTION_REQUIRED", message, retryable: false }} />)
  expect(screen.getByText(message)).toBeTruthy()
  expect(container.querySelector("details")).toBeNull()
  expect(screen.queryByText("assistant.continueAfterError")).toBeNull()
  expect(screen.queryByText("meta.regenerate")).toBeNull()
  expect(screen.getByRole("link", { name: "workbench:activation.subscribe" }).getAttribute("href")).toContain("billing")
})

it("does not offer regenerate for a subscription-blocked ordinary conversation", () => {
  preserve.mockReturnValue(false)
  render(<InlineErrorCard sessionId="task" messageId="reply" streaming={false}
    error={{ code: "SANDBOX_SUBSCRIPTION_REQUIRED", message: "请先续订套餐", retryable: false }} />)
  expect(screen.getByText("请先续订套餐")).toBeTruthy()
  expect(screen.queryByText("meta.regenerate")).toBeNull()
  expect(screen.getByText("meta.dismissTurn")).toBeTruthy()
})
