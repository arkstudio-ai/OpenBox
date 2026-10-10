import { describe, expect, it } from "vitest"
import { assetIdFromLink } from "./asset-link"

describe("assetIdFromLink", () => {
  const id = "asset_01M4CS5QBVAR93CR095J3KA7A0"

  it("recognises the tool's tokenised download link with or without a host", () => {
    for (const href of [
      `/api/assets/${id}/download?token=expired.sig`,
      `https://ai.bossipai.com.cn/api/assets/${id}/download?token=x`,
      `/api/assets/${id}/url`,
    ]) {
      expect(assetIdFromLink(href)).toBe(id)
    }
  })

  it("leaves other links alone", () => {
    for (const href of [
      "/app/s/session_1",
      "/api/assets/asset_1/text",
      "/api/assets?project=all",
      "https://example.com/api/assets/asset_1/download/../x",
      undefined,
    ]) {
      expect(assetIdFromLink(href)).toBeNull()
    }
  })
})
