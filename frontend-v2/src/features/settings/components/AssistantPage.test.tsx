import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { ApiError, http } from "@/shared/api/http"
import { useAppearanceStore } from "@/shared/appearance/store"
import { toast } from "@/shared/ui/Toast"
import { AssistantPage } from "./AssistantPage"

vi.mock("react-i18next", async (original) => ({
  ...await original<typeof import("react-i18next")>(),
  useTranslation: () => ({
    t: (key: string, opts?: Record<string, unknown>) => (opts?.name ? `${key}:${opts.name}` : key),
  }),
}))
vi.mock("@/shared/ui/Toast", () => ({ toast: vi.fn() }))

beforeEach(() => {
  useAppearanceStore.setState({ assistantName: "" })
})
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe("AssistantPage", () => {
  it("saves the typed name through the server and shows what it kept", async () => {
    const put = vi.spyOn(http, "put").mockResolvedValue({ name: "Mary" })
    render(<AssistantPage />)
    const save = screen.getByRole("button", { name: "assistant.save" })
    expect((save as HTMLButtonElement).disabled).toBe(true) // nothing changed yet
    fireEvent.change(screen.getByRole("textbox", { name: "assistant.nameLabel" }), { target: { value: " Mary " } })
    fireEvent.click(save)
    await waitFor(() => expect(put).toHaveBeenCalledWith("/api/assistant/name", { name: " Mary " }))
    await waitFor(() => expect(useAppearanceStore.getState().assistantName).toBe("Mary"))
    expect(toast).toHaveBeenCalledWith("success", "assistant.saved:Mary")
    // The field follows the stored name; the default can be restored.
    expect((screen.getByRole("textbox") as HTMLInputElement).value).toBe("Mary")
    put.mockResolvedValue({ name: "" })
    fireEvent.click(screen.getByRole("button", { name: "assistant.reset" }))
    await waitFor(() => expect(useAppearanceStore.getState().assistantName).toBe(""))
    expect(toast).toHaveBeenLastCalledWith("success", "assistant.resetDone")
  })

  it("explains a refused name and keeps the current one", async () => {
    useAppearanceStore.setState({ assistantName: "Mary" })
    vi.spyOn(http, "put").mockRejectedValue(new ApiError(422, "ASSISTANT_NAME_INVALID", "A name is one line of up to 20 characters"))
    render(<AssistantPage />)
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "x".repeat(20) + "y" } })
    fireEvent.click(screen.getByRole("button", { name: "assistant.save" }))
    await waitFor(() => expect(toast).toHaveBeenCalledWith("error", "assistant.invalid"))
    expect(useAppearanceStore.getState().assistantName).toBe("Mary")
  })
})
