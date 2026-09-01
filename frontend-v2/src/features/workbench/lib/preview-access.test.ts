import { describe, expect, it, vi } from "vitest"
import { assertCleanPreviewUrl, authorizePreviewNavigation } from "./preview-access"

describe("preview access contract", () => {
  const valid = "/api/containers/box-1/preview/3000/"

  it("authorizes before navigating to the clean path", async () => {
    const request = vi.fn().mockResolvedValue({ url: valid, mode: "sandboxed_same_origin" })
    const navigate = vi.fn()

    await expect(authorizePreviewNavigation(request, "box-1", 3000, navigate)).resolves.toEqual({
      url: valid,
      isolated: false,
    })
    expect(request).toHaveBeenCalledWith("box-1", 3000)
    expect(navigate).toHaveBeenCalledWith({ url: valid, isolated: false })
  })

  it.each([
    "https://attacker.example/api/containers/box-1/preview/3000/",
    "//attacker.example/api/containers/box-1/preview/3000/",
    "/api/containers/box-1/preview/3000/?_pt=secret",
    "/api/containers/box-1/preview/3000/#secret",
    "/api/containers/box-2/preview/3000/",
    "/api/containers/box-1/preview/3001/",
    "api/containers/box-1/preview/3000/",
  ])("rejects a non-contract URL: %s", (url) => {
    expect(() =>
      assertCleanPreviewUrl({ url, mode: "sandboxed_same_origin" }, "box-1", 3000),
    ).toThrow("preview_url_contract")
  })

  it("enables full app capability only for a distinct HTTPS origin", () => {
    expect(
      assertCleanPreviewUrl(
        {
          url: "https://preview.example.test/api/containers/box-1/preview/3000/",
          mode: "isolated_origin",
        },
        "box-1",
        3000,
        "https://app.example.test",
      ),
    ).toEqual({
      url: "https://preview.example.test/api/containers/box-1/preview/3000/",
      isolated: true,
    })

    expect(() =>
      assertCleanPreviewUrl(
        {
          url: "https://app.example.test/api/containers/box-1/preview/3000/",
          mode: "isolated_origin",
        },
        "box-1",
        3000,
        "https://app.example.test",
      ),
    ).toThrow("preview_url_contract")
  })
})
