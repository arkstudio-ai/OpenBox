import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { usePayload, type PayloadContent } from "../../api/queries"
import { useDownloadPayload } from "../../hooks/useDownloadPayload"
import { useOpenPayloadLink } from "../../hooks/useOpenPayloadLink"
import { inspectorEnv, withInspector } from "../testing/harness"
import { PayloadView } from "./PayloadView"

vi.mock("react-i18next", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react-i18next")>()
  const t = (key: string, vars?: Record<string, unknown>) => (vars ? `${key} ${JSON.stringify(vars)}` : key)
  return { ...actual, useTranslation: () => ({ t, i18n: { language: "en-US" } }) }
})
vi.mock("../../hooks/useDownloadPayload", () => ({ useDownloadPayload: vi.fn() }))
vi.mock("../../hooks/useOpenPayloadLink", () => ({ useOpenPayloadLink: vi.fn() }))
vi.mock("../../api/queries", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../api/queries")>()),
  usePayload: vi.fn(),
}))

const VIDEO: PayloadContent = {
  availability: "available",
  mediaType: "video/mp4",
  size: 120_000_000,
  blob: new Blob(["mp4!"], { type: "video/mp4" }),
  text: null,
}
const open = vi.fn(() => Promise.resolve({ opened: true as const, expiresIn: 600 }))

beforeEach(() => {
  Object.assign(URL, { createObjectURL: () => "blob:fixture", revokeObjectURL: () => undefined })
  vi.mocked(usePayload).mockReturnValue({ data: VIDEO, error: null } as unknown as ReturnType<typeof usePayload>)
  vi.mocked(useDownloadPayload).mockReturnValue({ mutate: vi.fn(), isPending: false, error: null } as unknown as ReturnType<typeof useDownloadPayload>)
  vi.mocked(useOpenPayloadLink).mockReturnValue({ open, isPending: false, error: null, data: undefined } as unknown as ReturnType<typeof useOpenPayloadLink>)
  open.mockClear()
})
afterEach(cleanup)

describe("PayloadView open-original action", () => {
  it("is offered only for payloads that reference the person's own object", () => {
    const { unmount } = render(withInspector(<PayloadView reference={{ payload_id: "pl_copy" }} />, inspectorEnv()))
    expect(screen.queryByTestId("trajectory-payload-open")).toBeNull()
    unmount()
    render(withInspector(<PayloadView reference={{ payload_id: "pl_vid" }} filename="成片.mp4" openable />, inspectorEnv()))
    expect(screen.getByTestId("trajectory-payload-download")).toBeTruthy()
    fireEvent.click(screen.getByTestId("trajectory-payload-open"))
    expect(open).toHaveBeenCalledWith({ filename: "成片.mp4" })
  })

  it("says why when the server refused a link", () => {
    vi.mocked(useOpenPayloadLink).mockReturnValue({
      open, isPending: false, error: null, data: { opened: false, reason: "not_linkable" },
    } as unknown as ReturnType<typeof useOpenPayloadLink>)
    render(withInspector(<PayloadView reference={{ payload_id: "pl_copy" }} openable />, inspectorEnv()))
    expect(screen.getByRole("status").textContent).toBe("payload.linkRefused.not_linkable")
  })
})
