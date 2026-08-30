import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { cleanup, render, screen } from "@testing-library/react"
import { useAssetUrl } from "@/shared/api/assets"
import type { MessagePart, SkillJobPart } from "@/shared/types/api"
import { SkillJobReceipts } from "./SkillJobReceipts"

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string) =>
      ({
        "status.succeeded": "Completed",
        "status.failed": "Failed",
        "status.cancelled": "Cancelled",
        "state.unavailable": "Unavailable",
      })[key] ?? key,
  }),
}))

vi.mock("@/shared/api/assets", () => ({ useAssetUrl: vi.fn() }))

const assetResponses = {
  "asset-video": {
    url: "https://assets.test/video.mp4",
    mime: "video/mp4",
    name: "video.mp4",
    size: 10,
  },
  "asset-image": {
    url: "https://assets.test/image.png",
    mime: "image/png",
    name: "image.png",
    size: 20,
  },
  "asset-file": {
    url: "https://assets.test/notes.txt",
    mime: "text/plain",
    name: "notes.txt",
    size: 30,
  },
  "asset-api-name": {
    url: "https://assets.test/from-api.pdf",
    mime: "application/pdf",
    name: "from-api.pdf",
    size: 40,
  },
} as const

function queryResult(assetId: string | null | undefined) {
  const data =
    assetId && assetId in assetResponses ? assetResponses[assetId as keyof typeof assetResponses] : undefined
  return {
    data,
    isError: assetId === "asset-dead",
    isSuccess: Boolean(data),
  } as unknown as ReturnType<typeof useAssetUrl>
}

function receipt(overrides: Partial<SkillJobPart> = {}): SkillJobPart {
  return {
    type: "skill_job",
    id: "receipt-1",
    jobId: "job-1",
    skillKey: "builtin:video-production",
    operation: "segment.generate",
    status: "succeeded",
    ...overrides,
  }
}

describe("SkillJobReceipts", () => {
  beforeEach(() => {
    vi.mocked(useAssetUrl).mockImplementation(queryResult)
  })

  afterEach(() => {
    cleanup()
    vi.clearAllMocks()
  })

  it("renders a complete receipt and video, image, and ordinary artifacts", () => {
    const part = receipt({
      artifacts: [
        { assetId: "asset-video", name: "video.mp4", mime: "video/mp4" },
        { assetId: "asset-image", name: "image.png" },
        { assetId: "asset-file", name: "notes.txt", mime: "text/plain" },
      ],
    })

    const { container } = render(<SkillJobReceipts parts={[part]} />)

    expect(screen.getByText("video-production · segment.generate")).toBeTruthy()
    expect(screen.getByText("Completed")).toBeTruthy()
    expect(container.querySelector("video")?.getAttribute("src")).toBe("https://assets.test/video.mp4")
    expect(screen.getByAltText("image.png").getAttribute("src")).toBe("https://assets.test/image.png")
    expect(screen.getByRole("link", { name: "notes.txt" }).getAttribute("href")).toBe(
      "https://assets.test/notes.txt",
    )
  })

  it("keeps a legacy pre-artifacts receipt readable without requesting an asset", () => {
    render(<SkillJobReceipts parts={[receipt({ artifacts: undefined })]} />)

    expect(screen.getByText("video-production · segment.generate")).toBeTruthy()
    expect(screen.getByText("Completed")).toBeTruthy()
    expect(useAssetUrl).not.toHaveBeenCalled()
  })

  it("shows unknown status verbatim and missing fields as unavailable without an empty separator", () => {
    const parts: MessagePart[] = [
      receipt({
        id: "receipt-unknown",
        skillKey: "user:custom-skill",
        operation: undefined,
        status: " timed_out ",
      }),
      receipt({
        id: "receipt-missing",
        skillKey: undefined,
        operation: undefined,
        status: undefined,
      }),
    ]

    const { container } = render(<SkillJobReceipts parts={parts} />)

    expect(screen.getByText("custom-skill")).toBeTruthy()
    expect(screen.getByText("timed_out")).toBeTruthy()
    expect(screen.getByText("Unavailable")).toBeTruthy()
    expect(screen.queryByText("Cancelled")).toBeNull()
    expect(container.textContent).not.toContain("custom-skill ·")
  })

  it("uses resolved asset metadata when embedded fields are missing and labels failures", () => {
    render(
      <SkillJobReceipts
        parts={[
          receipt({
            artifacts: [
              { assetId: "asset-api-name" },
              { assetId: "asset-dead", name: "old-video.mp4" },
              { name: "missing-id.mp4", mime: "video/mp4" },
              {},
            ],
          }),
        ]}
      />,
    )

    expect(screen.getByRole("link", { name: "from-api.pdf" })).toBeTruthy()
    expect(screen.getByText("old-video.mp4 · Unavailable")).toBeTruthy()
    expect(screen.getByText("missing-id.mp4 · Unavailable")).toBeTruthy()
    expect(screen.queryByText("Unavailable · Unavailable")).toBeNull()
  })
})
