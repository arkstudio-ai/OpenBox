import { act, cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { usePayload, type PayloadContent } from "../../api/queries"
import { useDownloadPayload } from "../../hooks/useDownloadPayload"
import { inspectorEnv, withInspector } from "../testing/harness"
import { MediaRefView } from "./MediaRefView"
import { MessageView } from "./MessageView"
import { PayloadView } from "./PayloadView"

vi.mock("react-i18next", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react-i18next")>()
  const t = (key: string, vars?: Record<string, unknown>) => (vars ? `${key} ${JSON.stringify(vars)}` : key)
  return { ...actual, useTranslation: () => ({ t, i18n: { language: "en-US" } }) }
})

vi.mock("../../hooks/useDownloadPayload", () => ({ useDownloadPayload: vi.fn() }))

vi.mock("../../api/queries", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../api/queries")>()),
  usePayload: vi.fn(),
}))

const payloadMock = vi.mocked(usePayload)
const downloadMock = vi.mocked(useDownloadPayload)
const mutate = vi.fn()
const createUrl = vi.fn(() => "blob:fixture-media")
const revokeUrl = vi.fn()

function payloadResult(data: PayloadContent | undefined) {
  return { data, error: null } as unknown as ReturnType<typeof usePayload>
}

const PNG: PayloadContent = {
  availability: "available",
  mediaType: "image/png",
  size: 4,
  blob: new Blob(["png!"], { type: "image/png" }),
  text: null,
}

/** Stands in for the browser's IntersectionObserver (jsdom has none); `report` delivers an observation. */
class FakeIntersectionObserver {
  static latest: FakeIntersectionObserver | null = null
  private readonly callback: IntersectionObserverCallback

  constructor(callback: IntersectionObserverCallback) {
    this.callback = callback
    FakeIntersectionObserver.latest = this
  }

  observe = () => undefined
  disconnect = () => undefined

  report(isIntersecting: boolean) {
    this.callback([{ isIntersecting } as IntersectionObserverEntry], this as unknown as IntersectionObserver)
  }
}

beforeEach(() => {
  Object.assign(URL, { createObjectURL: createUrl, revokeObjectURL: revokeUrl })
  createUrl.mockClear()
  revokeUrl.mockClear()
  payloadMock.mockReset()
  mutate.mockClear()
  downloadMock.mockReturnValue({ mutate, isPending: false, error: null } as unknown as ReturnType<
    typeof useDownloadPayload
  >)
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  FakeIntersectionObserver.latest = null
})

describe("retained model input media", () => {
  it("previews a nested $media image through the protected payload read and leaves remote URLs inert", () => {
    payloadMock.mockReturnValue(payloadResult(PNG))
    const message = {
      role: "user",
      content: [
        {
          type: "image_url",
          image_url: {
            url: {
              $media: {
                payload_id: "pl_input",
                media_type: "image/png",
                size_bytes: 4,
                availability: "available",
              },
              source_kind: "inline_base64",
              original_encoding: "base64",
            },
          },
        },
        { type: "image_url", image_url: { url: "https://remote.example/cat.png" } },
      ],
    }
    const { container, unmount } = render(
      withInspector(<MessageView message={message} index={0} />, inspectorEnv()),
    )
    expect(payloadMock).toHaveBeenCalledWith("ses_test", "10", "pl_input", {
      revalidation: "body",
      shown: true,
    })
    const images = container.querySelectorAll("img")
    expect(images).toHaveLength(1)
    expect(images[0].getAttribute("src")).toBe("blob:fixture-media")
    expect(container.querySelector('img[src^="https:"]')).toBeNull()
    unmount()
    expect(revokeUrl).toHaveBeenCalledWith("blob:fixture-media")
  })

  it("states why media without retained bytes is unavailable, without reading anything", () => {
    payloadMock.mockReturnValue(payloadResult(undefined))
    const { container } = render(
      withInspector(
        <>
          <MediaRefView
            value={{ $media: { availability: "not_recorded", reason: "ambiguous_source" } }}
            autoLoad
          />
          <MediaRefView
            value={{
              $media: { payload_id: "pl_gone", availability: "deleted", reason: "explicitly_deleted" },
            }}
            autoLoad
          />
          <MediaRefView value={{ $media: { availability: "available" } }} autoLoad />
        </>,
        inspectorEnv(),
      ),
    )
    const states = [...container.querySelectorAll("[data-testid=trajectory-media-ref]")].map((node) =>
      node.getAttribute("data-state"),
    )
    expect(states).toEqual(["not_recorded", "deleted", "not_recorded"])
    expect(container.querySelector("[data-availability=deleted]")?.textContent).toContain(
      "explicitly_deleted",
    )
    expect(payloadMock).not.toHaveBeenCalledWith("ses_test", "10", "pl_gone")
    expect(container.querySelector("img")).toBeNull()
  })

  it("releases a shown image when revalidation finds it deleted", () => {
    payloadMock.mockReturnValue(payloadResult(PNG))
    const env = inspectorEnv()
    const { container, rerender } = render(
      withInspector(<PayloadView reference={{ payload_id: "pl_input" }} />, env),
    )
    expect(container.querySelector("img")).not.toBeNull()
    fireEvent.click(screen.getByTestId("trajectory-payload-download"))
    expect(mutate).toHaveBeenCalledWith({ filename: null })
    expect(downloadMock).toHaveBeenCalledWith("ses_test", "10", "pl_input")
    payloadMock.mockReturnValue(
      payloadResult({ availability: "deleted", mediaType: null, size: null, blob: null, text: null }),
    )
    rerender(withInspector(<PayloadView reference={{ payload_id: "pl_input" }} />, env))
    expect(container.querySelector("img")).toBeNull()
    expect(container.querySelector("[data-availability=deleted]")).not.toBeNull()
    expect(screen.queryByTestId("trajectory-payload-download")).toBeNull()
    expect(revokeUrl).toHaveBeenCalledWith("blob:fixture-media")
  })

  it("revalidates shown content with the availability check where the server offers one", () => {
    payloadMock.mockReturnValue(payloadResult(PNG))
    render(
      withInspector(<PayloadView reference={{ payload_id: "pl_input" }} />, inspectorEnv([], { refs: true })),
    )
    expect(payloadMock).toHaveBeenCalledWith("ses_test", "10", "pl_input", {
      revalidation: "meta",
      shown: true,
    })
  })

  it("pauses that check while the content is scrolled out of view", () => {
    vi.stubGlobal("IntersectionObserver", FakeIntersectionObserver)
    payloadMock.mockReturnValue(payloadResult(PNG))
    render(
      withInspector(<PayloadView reference={{ payload_id: "pl_input" }} />, inspectorEnv([], { refs: true })),
    )
    const options = () => payloadMock.mock.lastCall?.[3]
    expect(options()).toEqual({ revalidation: "meta", shown: true })
    act(() => FakeIntersectionObserver.latest?.report(false))
    expect(options()).toEqual({ revalidation: "meta", shown: false })
    act(() => FakeIntersectionObserver.latest?.report(true))
    expect(options()).toEqual({ revalidation: "meta", shown: true })
  })
})
