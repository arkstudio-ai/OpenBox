import { cleanup, render, screen } from "@testing-library/react"
import { MemoryRouter } from "react-router"
import { afterEach, expect, it, vi } from "vitest"
import type { Session } from "@/shared/types/api"
import { roomState } from "../lib/room-state"
import { TaskRoomBanner } from "./TaskRoomBanner"

vi.mock("react-i18next", () => ({ useTranslation: () => ({ t: (key: string, options?: { title?: string }) =>
  options?.title ? `${key}:${options.title}` : key }) }))

const session = {
  id: "execution", title: "会话标题", status: "idle", assistant_managed: true, kind: "normal",
  task_control: { task_id: "t1", expected_revision: 3, expected_run: null, desired_state: "running", observed_state: "running",
    task_title: "艾薇兰小红瓶调研" },
} as unknown as Session

afterEach(cleanup)

it("names the room by its task title, shows its state and links back to the assistant", () => {
  render(<MemoryRouter><TaskRoomBanner session={session} /></MemoryRouter>)
  expect(screen.getByText("room.here:艾薇兰小红瓶调研")).toBeTruthy()
  expect(screen.getByText("room.state.running")).toBeTruthy()
  expect(screen.getByRole("link", { name: "room.back" }).getAttribute("href")).toBe("/app/assistant")
})

it("stays off the assistant's own conversation and off ordinary chats", () => {
  render(<MemoryRouter><TaskRoomBanner session={{ ...session, kind: "assistant" }} /></MemoryRouter>)
  expect(screen.queryByTestId("task-room-banner")).toBeNull()
  cleanup()
  render(<MemoryRouter><TaskRoomBanner session={{ ...session, assistant_managed: false, task_control: null }} /></MemoryRouter>)
  expect(screen.queryByTestId("task-room-banner")).toBeNull()
})

it("falls back to the session title on older servers and reads the state from both fields", () => {
  const control = session.task_control!
  render(<MemoryRouter><TaskRoomBanner session={{ ...session, task_control: { ...control, task_title: undefined } }} /></MemoryRouter>)
  expect(screen.getByText("room.here:会话标题")).toBeTruthy()
  expect(roomState({ ...control, desired_state: "canceled", observed_state: "effect_unknown" })).toBe("effect_unknown")
  expect(roomState({ ...control, desired_state: "canceled", observed_state: "idle" })).toBe("canceled")
  expect(roomState({ ...control, desired_state: "paused", observed_state: "resume_blocked" })).toBe("paused")
  expect(roomState({ ...control, observed_state: "waiting_input" })).toBe("waiting")
})
