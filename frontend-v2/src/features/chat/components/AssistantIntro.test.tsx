import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { http } from "@/shared/api/http"
import { useAuthStore } from "@/shared/api/auth-store"
import {
  DEFAULT_ASSISTANT_META,
  DEFAULT_ASSISTANT_PROFILE,
  type AssistantMeta,
} from "@/shared/appearance/assistant-profile"
import { useAppearanceStore } from "@/shared/appearance/store"
import { AssistantIntro } from "./AssistantIntro"

vi.mock("react-i18next", async (original) => ({
  ...(await original<typeof import("react-i18next")>()),
  useTranslation: () => ({
    t: (key: string, opts?: Record<string, unknown>) => {
      if (opts?.returnObjects) return [{ title: `${key}.first`, prompt: `${key}.prompt` }]
      const shown = Object.entries(opts ?? {}).map(([name, value]) => `${name}=${String(value)}`)
      return shown.length ? `${key}(${shown.join(",")})` : key
    },
  }),
}))
vi.mock("@/shared/ui/Toast", () => ({ toast: vi.fn() }))

/** The server's answer to each step: the meeting moves on as it would. */
function server(meta: AssistantMeta = DEFAULT_ASSISTANT_META) {
  const profile = { ...DEFAULT_ASSISTANT_PROFILE }
  const state = structuredClone(meta)
  return vi.spyOn(http, "post").mockImplementation(async (_url, body) => {
    const event = body as { event: string; step?: string; value?: string }
    if (event.event === "answer" && event.step) {
      Object.assign(profile, { [event.step]: event.value })
      state.decided[event.step as keyof typeof profile] = { at: null, via: "intro" }
      state.intro.steps[event.step as "address"] = "answered"
    }
    if (event.event === "skip" && event.step) state.intro.steps[event.step as "name"] = "skipped"
    if (event.event === "dismiss") state.intro.status = "dismissed"
    return { ...profile, decided: state.decided, intro: state.intro }
  })
}

beforeEach(() => {
  useAuthStore.setState({ user: { id: "u", username: "memoryqa_2026", role: "user" } as never })
  useAppearanceStore.setState({
    assistant: DEFAULT_ASSISTANT_PROFILE,
    assistantMeta: structuredClone(DEFAULT_ASSISTANT_META),
  })
})
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe("AssistantIntro", () => {
  it("asks only what is undecided, one question at a time, and ends with one real thing to start", async () => {
    const meta = structuredClone(DEFAULT_ASSISTANT_META)
    meta.decided.length = { at: null, via: "settings" } // chosen in Settings before the meeting
    useAppearanceStore.setState({ assistantMeta: meta })
    const post = server(meta)
    const onPick = vi.fn()
    const onClose = vi.fn()
    render(<AssistantIntro mode="auto" onPick={onPick} onClose={onClose} />)
    expect(screen.getByText("assistant.intro.lead")).toBeTruthy()
    expect(screen.getByText("assistant.intro.progress(current=1,total=3)")).toBeTruthy()
    // The sign-in name is offered, never filled in.
    expect(
      screen.getByRole("button", { name: "assistant.intro.address.useAccount(name=memoryqa_2026)" }),
    ).toBeTruthy()
    const field = screen.getByRole("textbox", {
      name: "assistant.intro.address.question",
    }) as HTMLInputElement
    expect(field.value).toBe("")
    fireEvent.change(field, { target: { value: " 老王 " } })
    fireEvent.click(screen.getByRole("button", { name: "assistant.intro.confirm" }))
    await waitFor(() =>
      expect(post).toHaveBeenLastCalledWith("/api/assistant/intro", {
        event: "answer",
        step: "address",
        value: "老王",
      }),
    )
    expect(await screen.findByText("assistant.intro.name.question")).toBeTruthy()
    fireEvent.click(screen.getByRole("button", { name: "assistant.intro.skip" }))
    await waitFor(() =>
      expect(post).toHaveBeenLastCalledWith("/api/assistant/intro", { event: "skip", step: "name" }),
    )
    expect(await screen.findByText("assistant.intro.business.question")).toBeTruthy()
    expect(screen.queryByText("assistant.intro.length.question")).toBeNull() // decided in Settings: not asked
    fireEvent.click(screen.getByRole("button", { name: "assistant.intro.business.beauty" }))
    // Done: greeted by the new address, with things to start that fit the business.
    expect(await screen.findByText("assistant.intro.doneTitle(name=老王)")).toBeTruthy()
    fireEvent.click(screen.getByRole("button", { name: "assistant.intro.suggest.beauty.first" }))
    expect(onPick).toHaveBeenCalledWith("assistant.intro.suggest.beauty.prompt")
    expect(onClose).toHaveBeenCalled()
  })

  it("puts the meeting off with 以后再说; one opened later just closes", async () => {
    const post = server()
    const onClose = vi.fn()
    const { unmount } = render(<AssistantIntro mode="auto" onPick={vi.fn()} onClose={onClose} />)
    fireEvent.click(screen.getByRole("button", { name: "assistant.intro.later" }))
    await waitFor(() => expect(post).toHaveBeenCalledWith("/api/assistant/intro", { event: "dismiss" }))
    expect(onClose).toHaveBeenCalledTimes(1)
    unmount()
    render(<AssistantIntro mode="undecided" onPick={vi.fn()} onClose={onClose} />)
    expect(screen.queryByRole("button", { name: "assistant.intro.later" })).toBeNull()
    fireEvent.click(screen.getByRole("button", { name: "assistant.intro.close" }))
    expect(onClose).toHaveBeenCalledTimes(2)
    expect(post).toHaveBeenCalledTimes(1) // closing one the person opened records nothing
  })

  it("going through again shows today's choices and asks every question", () => {
    const meta = structuredClone(DEFAULT_ASSISTANT_META)
    meta.decided.address = { at: null, via: "intro" }
    meta.decided.length = { at: null, via: "settings" }
    useAppearanceStore.setState({
      assistant: { ...DEFAULT_ASSISTANT_PROFILE, address: "老王", length: "brief" },
      assistantMeta: meta,
    })
    render(<AssistantIntro mode="all" onPick={vi.fn()} onClose={vi.fn()} />)
    expect(screen.getByText("assistant.intro.progress(current=1,total=4)")).toBeTruthy()
    expect((screen.getByRole("textbox") as HTMLInputElement).value).toBe("老王")
  })

  it("skips a question decided elsewhere while it was open", async () => {
    server()
    render(<AssistantIntro mode="auto" onPick={vi.fn()} onClose={vi.fn()} />)
    expect(screen.getByText("assistant.intro.address.question")).toBeTruthy()
    const meta = structuredClone(DEFAULT_ASSISTANT_META)
    meta.decided.address = { at: null, via: "chat" } // said on the phone meanwhile
    useAppearanceStore.setState({ assistantMeta: meta })
    expect(await screen.findByText("assistant.intro.name.question")).toBeTruthy()
  })
})
