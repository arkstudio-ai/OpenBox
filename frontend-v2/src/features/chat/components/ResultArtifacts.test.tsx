import { afterEach, describe, expect, it, vi } from "vitest"
import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import type { FilePart } from "@/shared/types/api"
import type { ArtifactGroup } from "../lib/content-view"
import { buildAssistantContentView } from "../lib/content-view"
import type { MessageWithParts } from "@/shared/types/api"
import directTranscript from "../lib/__fixtures__/direct-video-messages.json"
import { ResultArtifacts } from "./ResultArtifacts"

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string, vars?: { count?: number; number?: number }) =>
      key === "artifacts.segmentCollection"
        ? `Segments · ${vars?.count}`
        : key === "artifacts.segment"
          ? `Segment ${vars?.number}`
          : key,
  }),
}))
vi.mock("./AttachmentGallery", () => ({
  AttachmentGallery: ({ parts }: { parts: FilePart[] }) => (
    <div>
      {parts.map((p) => (
        <span key={p.id}>{p.path}</span>
      ))}
    </div>
  ),
}))

function group(
  id: string,
  artifactKind = "video_segment",
  overrides: Partial<ArtifactGroup> = {},
): ArtifactGroup {
  return {
    kind: "artifact",
    id,
    order: 0,
    artifactKind,
    role: artifactKind === "video_final" ? "final" : "intermediate",
    label: null,
    caption: null,
    ordinal: null,
    revision: null,
    metadata: {},
    sourceTool: null,
    parts: [{ type: "file", id, path: `${id}.mp4`, asset_id: id, mime_type: "video/mp4" }],
    ...overrides,
  }
}
const segments = [
  group("one", "video_segment", { ordinal: 1 }),
  group("two", "video_segment", { ordinal: 2 }),
]
const final = group("final", "video_final")
const toggle = () => screen.queryByRole("button", { name: /Segments/ })

afterEach(cleanup)

describe("video result layout", () => {
  it("keeps segments expanded with no collapse control before a final video exists", () => {
    render(<ResultArtifacts groups={segments} verification={null} />)
    expect(screen.getByText("one.mp4")).toBeTruthy()
    expect(screen.getByText("two.mp4")).toBeTruthy()
    expect(toggle()).toBeNull()
  })

  it("shows the folded collection above the final and allows manual expansion", () => {
    render(<ResultArtifacts groups={[final, ...segments]} verification={null} />)
    expect(toggle()?.getAttribute("aria-expanded")).toBe("false")
    expect(screen.queryByText("one.mp4")).toBeNull()
    expect(screen.getByText("final.mp4")).toBeTruthy()
    expect(
      toggle()!.compareDocumentPosition(screen.getByText("final.mp4")) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy()
    fireEvent.click(toggle()!)
    expect(toggle()?.getAttribute("aria-expanded")).toBe("true")
    expect(screen.getByText("one.mp4")).toBeTruthy()
    expect(
      screen.getByText("one.mp4").compareDocumentPosition(screen.getByText("final.mp4")) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy()
    fireEvent.click(toggle()!)
    expect(screen.queryByText("one.mp4")).toBeNull()
    expect(screen.getByText("final.mp4")).toBeTruthy()
  })

  it("folds when the final arrives during streaming, not only on the initial mount", () => {
    const { rerender } = render(<ResultArtifacts groups={segments} verification={null} />)
    rerender(<ResultArtifacts groups={[...segments, final]} verification={null} />)
    expect(toggle()?.getAttribute("aria-expanded")).toBe("false")
    expect(screen.queryByText("one.mp4")).toBeNull()
    fireEvent.click(toggle()!)
    rerender(<ResultArtifacts groups={[{ ...final }, ...segments]} verification={null} />)
    expect(toggle()?.getAttribute("aria-expanded")).toBe("true")
    expect(screen.getByText("one.mp4")).toBeTruthy()
  })

  it("expands again if the final is removed, and folds for a newly delivered revision", () => {
    const { rerender } = render(<ResultArtifacts groups={[...segments, final]} verification={null} />)
    fireEvent.click(toggle()!)
    rerender(<ResultArtifacts groups={[...segments, group("final-v2", "video_final")]} verification={null} />)
    expect(toggle()?.getAttribute("aria-expanded")).toBe("false")
    rerender(<ResultArtifacts groups={segments} verification={null} />)
    expect(toggle()).toBeNull()
    expect(screen.getByText("one.mp4")).toBeTruthy()
  })

  it.each([
    ["empty final placeholder", group("empty", "video_final", { parts: [] })],
    [
      "final without an uploaded asset",
      group("missing", "video_final", {
        parts: [{ type: "file", id: "missing", path: "missing.mp4", mime_type: "video/mp4" }],
      }),
    ],
    [
      "final image rather than video",
      group("image", "video_final", {
        parts: [{ type: "file", id: "image", path: "image.png", asset_id: "image", mime_type: "image/png" }],
      }),
    ],
    ["unrelated final deliverable", group("ordinary", "file", { role: "final", parts: [] })],
  ])("does not hide segments for %s", (_, other) => {
    render(<ResultArtifacts groups={[...segments, other]} verification={null} />)
    expect(toggle()).toBeNull()
    expect(screen.getByText("one.mp4")).toBeTruthy()
  })

  it("uses video semantics even for legacy final groups whose role is result", () => {
    render(<ResultArtifacts groups={[...segments, { ...final, role: "result" }]} verification={null} />)
    expect(toggle()?.getAttribute("aria-expanded")).toBe("false")
    expect(screen.getByText("final.mp4")).toBeTruthy()
  })

  it("never renders a segment twice even if legacy metadata calls it final", () => {
    render(<ResultArtifacts groups={[{ ...segments[0], role: "final" }]} verification={null} />)
    expect(screen.getAllByText("one.mp4")).toHaveLength(1)
    expect(toggle()).toBeNull()
  })

  it("has no empty collection for a final-only response", () => {
    render(<ResultArtifacts groups={[final]} verification={null} />)
    expect(screen.queryByText(/Segments/)).toBeNull()
    expect(screen.getByText("final.mp4")).toBeTruthy()
  })

  it("folds the real generate/share_file/final-answer shape only after delivery", () => {
    const messages = structuredClone(directTranscript) as MessageWithParts[]
    const project = (streaming: boolean) => buildAssistantContentView(messages, streaming).resultGroups
    const { rerender } = render(<ResultArtifacts groups={project(true)} verification={null} />)
    expect(toggle()).toBeNull()
    expect(screen.getByText("/workspace/generated_videos/segment-video_fixture.mp4")).toBeTruthy()
    rerender(<ResultArtifacts groups={project(false)} verification={null} />)
    expect(toggle()?.getAttribute("aria-expanded")).toBe("false")
    expect(screen.queryByText("/workspace/generated_videos/segment-video_fixture.mp4")).toBeNull()
    expect(screen.getByText("/workspace/uploads/segment-video_fixture.mp4")).toBeTruthy()
    fireEvent.click(toggle()!)
    rerender(<ResultArtifacts groups={project(false)} verification={null} />)
    expect(toggle()?.getAttribute("aria-expanded")).toBe("true")
  })

  it("honors explicitly final video attachments without requiring a particular tool kind", () => {
    render(
      <ResultArtifacts
        groups={[...segments, group("shared-final", "shared_file", { role: "final" })]}
        verification={null}
      />,
    )
    expect(toggle()?.getAttribute("aria-expanded")).toBe("false")
    expect(screen.queryByText("one.mp4")).toBeNull()
  })
})
