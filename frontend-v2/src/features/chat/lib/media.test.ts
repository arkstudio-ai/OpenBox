import { describe, expect, it } from "vitest"
import { isGalleryMedia, isVideoPart } from "./media"

describe("chat media parts", () => {
  it("renders an OSS-backed MP4 output in the preview gallery", () => {
    const part = { asset_id: "asset-final-video", mime_type: "video/mp4" }

    expect(isGalleryMedia(part)).toBe(true)
    expect(isVideoPart(part)).toBe(true)
  })

  it("keeps files without an owned asset out of the preview gallery", () => {
    expect(isGalleryMedia({ mime_type: "video/mp4" })).toBe(false)
  })
})
