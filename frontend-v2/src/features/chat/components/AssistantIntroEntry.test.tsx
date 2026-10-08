import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { MemoryRouter } from "react-router"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { http } from "@/shared/api/http"
import {
  DEFAULT_ASSISTANT_META,
  DEFAULT_ASSISTANT_PROFILE,
  type AssistantMeta,
} from "@/shared/appearance/assistant-profile"
import { useAppearanceStore } from "@/shared/appearance/store"
import { AssistantIntroEntry } from "./AssistantIntroEntry"

vi.mock("react-i18next", async (original) => ({
  ...(await original<typeof import("react-i18next")>()),
  useTranslation: () => ({
    t: (key: string, opts?: Record<string, unknown>) =>
      opts?.returnObjects ? [] : opts && "current" in opts ? `${key}(${opts.current}/${opts.total})` : key,
  }),
}))
vi.mock("@/shared/ui/Toast", () => ({ toast: vi.fn() }))

function meta(change: (value: AssistantMeta) => void = () => undefined): AssistantMeta {
  const value = structuredClone(DEFAULT_ASSISTANT_META)
  change(value)
  return value
}

function entry(quiet = true, path = "/app/assistant") {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <AssistantIntroEntry quiet={quiet} onPick={vi.fn()} />
    </MemoryRouter>,
  )
}

let post: ReturnType<typeof vi.spyOn>
beforeEach(() => {
  useAppearanceStore.setState({ assistant: DEFAULT_ASSISTANT_PROFILE, assistantMeta: meta() })
  post = vi.spyOn(http, "post").mockImplementation(async (_url, body) => ({
    ...DEFAULT_ASSISTANT_PROFILE,
    decided: {},
    intro: {
      ...DEFAULT_ASSISTANT_META.intro,
      status: (body as { event: string }).event === "dismiss" ? "dismissed" : "bypassed",
      nudged: true,
    },
  }))
})
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe("AssistantIntroEntry", () => {
  it("offers a quiet way back in while something is undecided, and only when the conversation is quiet", () => {
    const { unmount } = entry(false)
    expect(screen.queryByRole("button", { name: "assistant.intro.entry" })).toBeNull()
    unmount()
    entry()
    fireEvent.click(screen.getByRole("button", { name: "assistant.intro.entry" }))
    expect(screen.getByRole("dialog", { name: "assistant.intro.dialogTitle" })).toBeTruthy()
    expect(screen.getByText("assistant.intro.progress(1/4)")).toBeTruthy()
  })

  it("is gone once everything is decided", () => {
    useAppearanceStore.setState({
      assistantMeta: meta((value) => {
        for (const step of ["address", "name", "length", "business"] as const)
          value.decided[step] = { at: null, via: "settings" }
      }),
    })
    entry()
    expect(screen.queryByRole("button", { name: "assistant.intro.entry" })).toBeNull()
  })

  it("reminds once after the person went straight to work, and 不用了 puts it off for good", async () => {
    useAppearanceStore.setState({ assistantMeta: meta((value) => void (value.intro.status = "bypassed")) })
    entry()
    expect(screen.getByRole("status").textContent).toContain("assistant.intro.nudge")
    await waitFor(() => expect(post).toHaveBeenCalledWith("/api/assistant/intro", { event: "nudged" }))
    // Recorded as shown, it stays until answered.
    expect(screen.getByRole("status")).toBeTruthy()
    fireEvent.click(screen.getByRole("button", { name: "assistant.intro.nudgeNo" }))
    await waitFor(() => expect(post).toHaveBeenLastCalledWith("/api/assistant/intro", { event: "dismiss" }))
    expect(screen.queryByRole("status")).toBeNull()
  })

  it("opens every question from Settings' 重新认识一下, even mid-conversation", () => {
    useAppearanceStore.setState({
      assistantMeta: meta((value) => {
        for (const step of ["address", "name", "length", "business"] as const)
          value.decided[step] = { at: null, via: "settings" }
      }),
    })
    entry(false, "/app/assistant?intro=all")
    expect(screen.getByRole("dialog")).toBeTruthy()
    expect(screen.getByText("assistant.intro.progress(1/4)")).toBeTruthy()
  })
})
