import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react"
import type { ReactNode } from "react"
import { MemoryRouter, Route, Routes } from "react-router"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { ApiError, http } from "@/shared/api/http"
import { DEFAULT_ASSISTANT_PROFILE } from "@/shared/appearance/assistant-profile"
import { useAppearanceStore } from "@/shared/appearance/store"
import { toast } from "@/shared/ui/Toast"
import { AssistantPage } from "./AssistantPage"

vi.mock("react-i18next", async (original) => ({
  ...(await original<typeof import("react-i18next")>()),
  useTranslation: () => ({
    t: (key: string, opts?: Record<string, unknown>) => (opts ? `${key}:${JSON.stringify(opts)}` : key),
  }),
}))
vi.mock("@/shared/ui/Toast", () => ({ toast: vi.fn() }))

const learned = {
  learned: [{ id: "m1", revision: 3, summary: "用户嫌回答太长，希望先说结论", updated_at: null }],
  reactions: [{ reason: "too_long", count: 2 }],
}

function page(): ReactNode {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return (
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/app/settings/assistant"]}>
        <Routes>
          <Route path="/app/settings/assistant" element={<AssistantPage />} />
          <Route path="/app/assistant" element={<p>assistant-page</p>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  )
}

beforeEach(() => {
  useAppearanceStore.setState({ assistant: DEFAULT_ASSISTANT_PROFILE })
  vi.spyOn(http, "get").mockResolvedValue(learned)
})
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.mocked(toast).mockClear()
})

describe("AssistantPage", () => {
  it("saves only what changed, as the server keeps it, and every screen follows", async () => {
    const put = vi
      .spyOn(http, "put")
      .mockResolvedValue({ ...DEFAULT_ASSISTANT_PROFILE, name: "Mary", tone: "lively" })
    render(page())
    const save = screen.getByRole("button", { name: "assistant.save" })
    expect((save as HTMLButtonElement).disabled).toBe(true) // nothing changed yet
    fireEvent.change(screen.getByLabelText("assistant.nameLabel"), { target: { value: " Mary " } })
    fireEvent.click(screen.getByRole("button", { name: "assistant.tone.lively" }))
    fireEvent.click(screen.getByRole("button", { name: "assistant.tone.warm" })) // and back: not a change
    fireEvent.click(screen.getByRole("button", { name: "assistant.tone.lively" }))
    fireEvent.click(save)
    await waitFor(() =>
      expect(put).toHaveBeenCalledWith("/api/assistant/profile", { name: "Mary", tone: "lively" }),
    )
    await waitFor(() => expect(useAppearanceStore.getState().assistant.name).toBe("Mary"))
    expect(toast).toHaveBeenCalledWith("success", "assistant.saved")
    // The fields now show what is stored; clearing the name brings back the default.
    expect((screen.getByLabelText("assistant.nameLabel") as HTMLInputElement).value).toBe("Mary")
    expect(screen.getByRole("button", { name: "assistant.tone.lively" }).getAttribute("aria-pressed")).toBe(
      "true",
    )
    fireEvent.change(screen.getByLabelText("assistant.nameLabel"), { target: { value: "" } })
    put.mockResolvedValue({ ...DEFAULT_ASSISTANT_PROFILE, tone: "lively" })
    fireEvent.click(save)
    await waitFor(() => expect(put).toHaveBeenLastCalledWith("/api/assistant/profile", { name: "" }))
  })

  it("explains a refused profile and keeps the current one", async () => {
    useAppearanceStore.setState({ assistant: { ...DEFAULT_ASSISTANT_PROFILE, name: "Mary" } })
    vi.spyOn(http, "put").mockRejectedValue(new ApiError(422, "ASSISTANT_PROFILE_INVALID", "too long"))
    render(page())
    fireEvent.change(screen.getByLabelText("assistant.personaLabel"), { target: { value: "像个老朋友" } })
    fireEvent.click(screen.getByRole("button", { name: "assistant.save" }))
    await waitFor(() => expect(toast).toHaveBeenCalledWith("error", "assistant.invalid"))
    expect(useAppearanceStore.getState().assistant).toMatchObject({ name: "Mary", persona: "" })
  })

  it("shows what it learned on its own, each removable like any memory, and the reasons given", async () => {
    const post = vi.spyOn(http, "post").mockResolvedValue({})
    render(page())
    const section = await screen.findByRole("region", { name: "assistant.learned.title" })
    expect(within(section).getByText("用户嫌回答太长，希望先说结论")).toBeTruthy()
    expect(within(section).getByText(/assistant\.learned\.reaction:.*"count":2/)).toBeTruthy()
    fireEvent.click(within(section).getByRole("button", { name: "assistant.learned.remove" }))
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith(
        "/api/memories/m1/forget",
        expect.objectContaining({
          expected_revision: 3,
          mode: "memory",
        }),
      ),
    )
    expect(toast).toHaveBeenCalledWith("success", "assistant.learned.removed")
  })

  it("keeps the business as one of the kinds or in the person's own words", async () => {
    const put = vi.spyOn(http, "put").mockResolvedValue({ ...DEFAULT_ASSISTANT_PROFILE, business: "宠物店" })
    render(page())
    fireEvent.click(screen.getByRole("button", { name: "assistant.business.other" }))
    fireEvent.change(screen.getByRole("textbox", { name: "assistant.businessLabel" }), {
      target: { value: " 宠物店 " },
    })
    fireEvent.click(screen.getByRole("button", { name: "assistant.save" }))
    await waitFor(() => expect(put).toHaveBeenCalledWith("/api/assistant/profile", { business: "宠物店" }))
    fireEvent.click(screen.getByRole("button", { name: "assistant.business.food" }))
    fireEvent.click(screen.getByRole("button", { name: "assistant.save" }))
    await waitFor(() => expect(put).toHaveBeenLastCalledWith("/api/assistant/profile", { business: "food" }))
  })

  it("goes through the first meeting again from 重新认识一下", () => {
    render(page())
    fireEvent.click(screen.getByRole("button", { name: "assistant.restartIntro" }))
    expect(screen.getByText("assistant-page")).toBeTruthy()
  })
})
