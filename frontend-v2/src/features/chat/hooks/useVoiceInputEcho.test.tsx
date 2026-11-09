import { afterEach, describe, expect, it, vi } from "vitest"
import { renderHook } from "@testing-library/react"
import { emitAppEvent } from "@/shared/events/bus"
import { useStreamStore } from "../stores/stream"
import { useVoiceInputEcho } from "./useVoiceInputEcho"

const ids = () => (useStreamStore.getState().messages.get("main") ?? []).map((m) => m.id)

describe("useVoiceInputEcho", () => {
  afterEach(() => {
    useStreamStore.setState({ messages: new Map() })
    vi.useRealTimers()
  })

  it("shows the words as a pending bubble the server's message then replaces", () => {
    const { unmount } = renderHook(() => useVoiceInputEcho("main", true))
    emitAppEvent("chat.voice_input", { clientMessageId: "c1", text: "帮我查一下明天的天气" })
    emitAppEvent("chat.voice_input", { clientMessageId: "c1", text: "帮我查一下明天的天气" })
    expect(ids()).toEqual(["tmp-c1"])
    useStreamStore.getState().addMessage("main", { id: "m1", session_id: "main", role: "user", created_at: "",
      client_message_id: "c1", parts: [{ type: "text", id: "p1", text: "帮我查一下明天的天气" }] })
    expect(ids()).toEqual(["m1"])
    unmount()
  })

  it("does nothing in an ordinary conversation, and forgets a bubble nobody claimed", () => {
    vi.useFakeTimers()
    const ordinary = renderHook(() => useVoiceInputEcho("main", false))
    emitAppEvent("chat.voice_input", { clientMessageId: "c1", text: "x" })
    expect(ids()).toEqual([])
    const assistant = renderHook(() => useVoiceInputEcho("main", true))
    emitAppEvent("chat.voice_input", { clientMessageId: "c2", text: "y" })
    expect(ids()).toEqual(["tmp-c2"])
    vi.advanceTimersByTime(15 * 60_000 + 1)
    expect(ids()).toEqual([])
    ordinary.unmount()
    assistant.unmount()
  })
})
