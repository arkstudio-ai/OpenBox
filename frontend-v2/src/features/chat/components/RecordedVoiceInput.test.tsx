import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import { RecordedVoiceInput } from "./RecordedVoiceInput"
import type { RecordingDraft } from "../api/recorded-voice"

const fake = vi.hoisted(() => ({ start: vi.fn(), finish: vi.fn(), cancel: vi.fn(), transcribe: vi.fn() }))
vi.mock("react-i18next", () => ({ useTranslation: () => ({ t: (key: string) => key }) }))
vi.mock("../api/config", () => ({ useConfigQuery: () => ({ data: { voice_input_enabled: true } }) }))
vi.mock("@/shared/hooks/useApiErrorMessage", () => ({ useApiErrorMessage: () => (error: Error) => error.message }))
vi.mock("@/shared/audio/recording", () => ({ recordingSupported: () => true,
  DialogueRecorder: class { start = fake.start; finish = fake.finish; cancel = fake.cancel } }))
vi.mock("../api/recorded-voice", () => ({ transcribeRecording: (...args: unknown[]) => fake.transcribe(...args) }))

afterEach(cleanup)

beforeEach(() => {
  vi.clearAllMocks(); sessionStorage.clear()
  useAuthStore.setState({ user: { id: "owner" } as NonNullable<ReturnType<typeof useAuthStore.getState>["user"]> })
  useWorkspaceStore.setState({ currentId: "workspace" })
  fake.start.mockResolvedValue(undefined)
  fake.finish.mockReturnValue(new Blob([new Uint8Array(32044)], { type: "audio/wav" }))
  fake.transcribe.mockImplementation(async (draft: RecordingDraft, _scope: unknown, save: () => void) => {
    Object.assign(draft, { asset_id: "audio", text: "不要加辣" }); save(); return draft
  })
})
async function record() {
  fireEvent.click(screen.getByLabelText("voiceMessage.switchVoice"))
  const hold = screen.getByText("voiceMessage.hold")
  fireEvent.keyDown(hold, { key: " " })
  await waitFor(() => expect(screen.getByText(/voiceMessage.releaseSend/)).toBeTruthy())
  fireEvent.keyUp(hold, { key: " " })
}

describe("dialogue recording", () => {
  it("retries the same recording and client identity in the same conversation", async () => {
    const send = vi.fn().mockRejectedValueOnce(new Error("offline")).mockResolvedValue(undefined)
    render(<RecordedVoiceInput draftKey="chat-retry" onSend={send} />)
    await record()
    await screen.findByText("offline")
    fireEvent.click(screen.getByText("voiceMessage.retry"))
    await waitFor(() => expect(send).toHaveBeenCalledTimes(2))
    expect(send.mock.calls[0][0]).toBe("不要加辣")
    expect(send.mock.calls[1][1].client_id).toBe(send.mock.calls[0][1].client_id)
    expect(send.mock.calls[1][1].asset_id).toBe("audio")
    await waitFor(() => expect(sessionStorage.getItem("recording:owner:workspace:chat-retry")).toBeNull())
  })
  it("cancel and leaving the page never dispatch a message", async () => {
    const send = vi.fn()
    const page = render(<RecordedVoiceInput draftKey="chat-cancel" onSend={send} />)
    fireEvent.click(screen.getByLabelText("voiceMessage.switchVoice"))
    const hold = screen.getByText("voiceMessage.hold")
    fireEvent.keyDown(hold, { key: " " })
    await screen.findByText(/voiceMessage.releaseSend/)
    fireEvent.keyDown(hold, { key: "Escape" })
    expect(fake.transcribe).not.toHaveBeenCalled()
    fireEvent.keyDown(hold, { key: " " })
    page.unmount()
    expect(fake.cancel).toHaveBeenCalled()
    expect(send).not.toHaveBeenCalled()
  })
  it("an answer uses the current callback and stays a card draft", async () => {
    let resolve!: (draft: RecordingDraft) => void
    fake.transcribe.mockImplementation((draft: RecordingDraft) => new Promise((done) => {
      resolve = () => done(Object.assign(draft, { asset_id: "answer", text: "不要加辣" }))
    }))
    const first = vi.fn(), latest = vi.fn()
    const view = render(<RecordedVoiceInput draftKey="answer:q:0" answer onSend={first} />)
    fireEvent.click(screen.getByLabelText("voiceMessage.answer"))
    const hold = screen.getByText("voiceMessage.hold")
    fireEvent.keyDown(hold, { key: " " })
    await screen.findByText(/voiceMessage.releaseFill/)
    fireEvent.keyUp(hold, { key: " " })
    view.rerender(<RecordedVoiceInput draftKey="answer:q:0" answer onSend={latest} />)
    await act(async () => resolve({} as RecordingDraft))
    expect(first).not.toHaveBeenCalled()
    expect(latest).toHaveBeenCalledWith("不要加辣", expect.objectContaining({ asset_id: "answer" }))
    expect(screen.getByLabelText("voiceMessage.answer")).toBeTruthy()
  })
})
