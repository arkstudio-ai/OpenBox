import { describe, expect, it } from "vitest"
import { isGalleryMedia, isGeneratedMedia, isVideoPart } from "./media"

describe("chat media parts", () => {
  it.each(["generated_image", "video_segment", "video_final", "generated_audio"])(
    "recognizes explicit generation provenance: %s",
    (kind) => {
      expect(isGeneratedMedia({ type: "file", id: "f", path: "file", relation: { kind } })).toBe(true)
    },
  )

  it("excludes evidence and transient working bytes from generated media labels", () => {
    const part = { type: "file" as const, id: "f", path: "image.png", relation: { kind: "generated_image" } }
    expect(isGeneratedMedia({ ...part, transient: true })).toBe(false)
    expect(isGeneratedMedia({ ...part, relation: { ...part.relation, role: "evidence" } })).toBe(false)
    expect(isGeneratedMedia({ ...part, transient: true }, "video_final")).toBe(false)
    expect(isGeneratedMedia({ ...part, relation: { role: "evidence" } }, "video_final")).toBe(false)
  })
  it("renders an OSS-backed MP4 output in the preview gallery", () => {
    const part = { asset_id: "asset-final-video", mime_type: "video/mp4" }

    expect(isGalleryMedia(part)).toBe(true)
    expect(isVideoPart(part)).toBe(true)
  })

  it("keeps files without an owned asset out of the preview gallery", () => {
    expect(isGalleryMedia({ mime_type: "video/mp4" })).toBe(false)
  })
})
