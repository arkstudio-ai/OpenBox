import { expect, test } from "@playwright/test"

for (const width of [320, 390, 1280]) {
  test(`direct video delivery folds materials at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 844 })
    const assetReads: string[] = []
    const errors: string[] = []
    page.on("pageerror", (error) => errors.push(error.message))
    await page.route("**/api/**", (route) => {
      const path = new URL(route.request().url()).pathname
      if (!path.startsWith("/api/")) return route.continue()
      const id = path.match(/^\/api\/assets\/([^/]+)\/url$/)?.[1]
      if (!id) return route.fulfill({ status: 404, json: {} })
      assetReads.push(id)
      return route.fulfill({ json: { url: `/fixture-video/${id}.mp4` } })
    })
    await page.route("**/fixture-video/*", (route) =>
      process.env.UI_QA_VIDEO
        ? route.fulfill({ path: process.env.UI_QA_VIDEO, contentType: "video/mp4" })
        : route.fulfill({ contentType: "video/mp4", body: "" }),
    )
    await page.goto("/e2e/fixtures/video-results.html?direct=1")
    const segment = page.getByRole("heading", { name: "第 1 段", exact: true })
    const fold = page.getByRole("button", { name: /分段素材/ })
    await expect(segment).toBeVisible()
    await page.getByRole("button", { name: "模拟附件预览" }).click()
    await expect(segment).toBeVisible()
    await expect(fold).toHaveCount(0)
    await page.getByRole("button", { name: "模拟合成失败" }).click()
    await expect(segment).toBeVisible()
    await expect(fold).toHaveCount(0)
    await page.getByRole("button", { name: "模拟成片" }).click()
    await expect(fold).toHaveAttribute("aria-expanded", "false")
    await expect(segment).toHaveCount(0)
    const final = page.getByRole("heading", { name: "segment-video_fixture.mp4", exact: true })
    await expect(final).toBeVisible()
    expect((await fold.boundingBox())!.y).toBeLessThan((await final.boundingBox())!.y)
    await fold.click()
    await expect(segment).toBeVisible()
    await fold.click()
    await page.getByTitle("segment-video_fixture.mp4", { exact: true }).click()
    await expect(page.getByRole("dialog", { name: "segment-video_fixture.mp4" })).toBeVisible()
    await page.keyboard.press("Escape")
    await expect(segment).toHaveCount(0)
    assetReads.length = 0
    await page.goto("/e2e/fixtures/video-results.html?direct=1&final=1")
    await expect(fold).toHaveAttribute("aria-expanded", "false")
    await expect.poll(() => assetReads).toEqual(["asset-shared"])
    await page.reload()
    await expect(fold).toHaveAttribute("aria-expanded", "false")
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    await page.screenshot({ path: `test-results/direct-video-${width}-collapsed.png`, fullPage: true })
    expect(errors).toEqual([])
  })
}
