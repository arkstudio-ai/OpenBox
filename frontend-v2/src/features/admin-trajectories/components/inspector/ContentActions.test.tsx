// Copy and save build the file from the value on screen; a value that still
// carries `$ref` envelopes is read first, and nothing leaves the page when a
// reference cannot be read or access changed meanwhile.
import { QueryClient } from "@tanstack/react-query"
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { useAuthStore } from "@/shared/api/auth-store"
import { ApiError } from "@/shared/api/http"
import type { AuthUser } from "@/shared/types/api"
import { trajectoryApi } from "../../api/endpoints"
import { useTrajectoryAccess } from "../../stores/access"
import { saveBlob } from "../../utils/download"
import { inspectorEnv, withInspector } from "../testing/harness"
import { ContentActions } from "./ContentActions"

vi.mock("react-i18next", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react-i18next")>()
  const t = (key: string, vars?: Record<string, unknown>) => (vars ? `${key} ${JSON.stringify(vars)}` : key)
  return { ...actual, useTranslation: () => ({ t, i18n: { language: "en-US" } }) }
})
vi.mock("../../api/endpoints", () => ({ trajectoryApi: { blob: vi.fn() } }))
vi.mock("../../utils/download", () => ({ saveBlob: vi.fn() }))

const blob = vi.mocked(trajectoryApi.blob)
const writeText = vi.fn<(text: string) => Promise<void>>(async () => undefined)
const SHA = "c0".repeat(32)
const MESSAGES = [
  {
    $ref: {
      sha256: SHA,
      size_bytes: 40,
      media_type: "application/json",
      kind: "message",
      payload_id: "pld_1",
    },
  },
  { role: "user", content: "hi" },
]
const RESOLVED = [
  { role: "system", content: "Be precise" },
  { role: "user", content: "hi" },
]

function show(value: unknown, client = new QueryClient({ defaultOptions: { queries: { retry: false } } })) {
  return render(
    withInspector(
      <ContentActions value={value} name="request-messages" format="json" />,
      inspectorEnv([], { throughSeq: "37", refs: true }),
      client,
    ),
  )
}

beforeEach(() => {
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } })
  useTrajectoryAccess.setState({ epoch: 0, denied: null })
  useAuthStore.setState({
    user: { id: "admin-a", role: "admin" } as unknown as AuthUser,
    isAuthenticated: true,
    isLoading: false,
  })
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  blob.mockReset()
})

describe("copy and save", () => {
  it("copies a value without references at once and reads nothing", () => {
    show({ a: 1 })
    fireEvent.click(screen.getByRole("button", { name: "common.copyJson" }))
    expect(writeText).toHaveBeenCalledWith('{\n  "a": 1\n}')
    expect(blob).not.toHaveBeenCalled()
  })

  it("copies a captured look-alike as it is where the server offers no references", () => {
    render(
      withInspector(
        <ContentActions value={MESSAGES} name="request-messages" format="json" />,
        inspectorEnv([], { throughSeq: "37", refs: false }),
        new QueryClient({ defaultOptions: { queries: { retry: false } } }),
      ),
    )
    fireEvent.click(screen.getByRole("button", { name: "common.copyJson" }))
    expect(writeText).toHaveBeenCalledWith(JSON.stringify(MESSAGES, null, 2))
    fireEvent.click(screen.getByRole("button", { name: /^common\.download/ }))
    expect(saveBlob).toHaveBeenCalledTimes(1)
    expect(blob).not.toHaveBeenCalled()
    cleanup()

    // Outside an inspector there is no server to ask either.
    render(<ContentActions value={MESSAGES} name="request-messages" format="json" />)
    fireEvent.click(screen.getByRole("button", { name: "common.copyJson" }))
    expect(writeText).toHaveBeenLastCalledWith(JSON.stringify(MESSAGES, null, 2))
    expect(blob).not.toHaveBeenCalled()
  })

  it("reads references before copying or saving, each digest once, at the shown watermark", async () => {
    blob.mockResolvedValue({ role: "system", content: "Be precise" })
    show(MESSAGES)
    fireEvent.click(screen.getByRole("button", { name: "common.copyJson" }))
    await waitFor(() => expect(writeText).toHaveBeenCalled())
    expect(JSON.parse(writeText.mock.calls[0][0])).toEqual(RESOLVED)
    expect(blob).toHaveBeenCalledWith("ses_test", SHA, "37", expect.any(AbortSignal))

    fireEvent.click(screen.getByRole("button", { name: /^common\.download/ }))
    await waitFor(() => expect(saveBlob).toHaveBeenCalled())
    const [file, filename] = vi.mocked(saveBlob).mock.calls[0]
    expect(filename).toBe("request-messages.json")
    expect(JSON.parse(await file.text())).toEqual(RESOLVED)
    expect(blob).toHaveBeenCalledTimes(1)
  })

  it("says so and hands nothing out when a reference cannot be read", async () => {
    blob.mockRejectedValue(new ApiError(410, "trajectory_content_deleted", "deleted"))
    show(MESSAGES)
    fireEvent.click(screen.getByRole("button", { name: "common.copyJson" }))
    await waitFor(() => expect(screen.getByRole("alert").textContent).toContain("content.actionFailed"))
    expect(writeText).not.toHaveBeenCalled()
  })

  it("hands nothing out when access was lost while the references were read", async () => {
    let release!: (value: unknown) => void
    blob.mockImplementation(() => new Promise((resolve) => (release = resolve)))
    show(MESSAGES)
    fireEvent.click(screen.getByRole("button", { name: /^common\.download/ }))
    await waitFor(() => expect(blob).toHaveBeenCalled())
    act(() => useTrajectoryAccess.getState().revoke("forbidden", 403))
    await act(async () => release({ role: "system", content: "Be precise" }))
    await waitFor(() => expect(screen.getByRole("alert")).toBeTruthy())
    expect(saveBlob).not.toHaveBeenCalled()
  })
})
