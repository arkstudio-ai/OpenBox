import { expect, test } from "@playwright/test"

for (const width of [320, 390, 1280]) {
  test(`segments/final transition and layout at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 844 })
    const errors: string[] = []
    const assetReads: string[] = []
    page.on("pageerror", (error) => errors.push(error.message))
    await page.route("**/api/**", async (route) => {
      const path = new URL(route.request().url()).pathname
      if (!path.startsWith("/api/")) return route.continue()
      const id = path.match(/^\/api\/assets\/([^/]+)\/url$/)?.[1]
      if (id) {
        assetReads.push(id)
        return route.fulfill({ json: { url: `/fixture-video/${id}.mp4` } })
      }
      return route.fulfill({ status: 404, json: {} })
    })
    await page.route("**/fixture-video/*", (route) =>
      process.env.UI_QA_VIDEO
        ? route.fulfill({ path: process.env.UI_QA_VIDEO, contentType: "video/mp4" })
        : route.fulfill({ status: 200, contentType: "video/mp4", body: "" }),
    )
    await page.goto("/e2e/fixtures/video-results.html")
    const collection = page.getByRole("button", { name: /分段素材/ })
    const segment1 = page.getByRole("heading", { name: "第 1 段", exact: true })
    const segment2 = page.getByRole("heading", { name: "第 2 段", exact: true })
    const segment3 = page.getByRole("heading", { name: "第 3 段", exact: true })
    await expect(segment1).toBeVisible()
    await expect(collection).toHaveCount(0)
    expect((await segment1.boundingBox())!.y).toBeLessThanOrEqual((await segment2.boundingBox())!.y)
    expect((await segment2.boundingBox())!.y).toBeLessThan((await segment3.boundingBox())!.y)
    await page.getByRole("button", { name: "模拟合成失败" }).click()
    await expect(segment1).toBeVisible()
    await expect(collection).toHaveCount(0)
    await page.getByRole("button", { name: "模拟成片" }).click()
    await expect(collection).toHaveAttribute("aria-expanded", "false")
    await expect(segment1).toHaveCount(0)
    const final = page.getByRole("heading", { name: "最终视频", exact: true })
    await expect(final).toBeVisible()
    expect((await collection.boundingBox())!.y).toBeLessThan((await final.boundingBox())!.y)
    expect((await collection.boundingBox())!.height).toBeGreaterThanOrEqual(44)
    await page.screenshot({ path: `test-results/video-results-${width}-collapsed.png`, fullPage: true })
    await collection.focus()
    await page.keyboard.press("Enter")
    await expect(collection).toHaveAttribute("aria-expanded", "true")
    await expect(segment1).toBeVisible()
    expect((await segment3.boundingBox())!.y).toBeLessThan((await final.boundingBox())!.y)
    await page.getByTitle("segment-1.mp4", { exact: true }).click()
    await expect(page.getByRole("dialog", { name: "segment-1.mp4" })).toBeVisible()
    await page.keyboard.press("Escape")
    await expect(page.getByRole("dialog")).toHaveCount(0)
    await page.screenshot({ path: `test-results/video-results-${width}-expanded.png`, fullPage: true })
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    await collection.click()
    await page.getByTitle("final.mp4", { exact: true }).click()
    await expect(page.getByRole("dialog", { name: "final.mp4" })).toBeVisible()
    await page.keyboard.press("Escape")
    await expect(segment1).toHaveCount(0)
    await page.getByRole("button", { name: "移除成片" }).click()
    await expect(segment1).toBeVisible()
    await expect(collection).toHaveCount(0)
    // Reload a finished transcript: hidden segment cards must not fetch previews.
    assetReads.length = 0
    await page.goto("/e2e/fixtures/video-results.html?final=1")
    await expect(collection).toHaveAttribute("aria-expanded", "false")
    await expect.poll(() => assetReads).toEqual(["final"])
    await page.reload()
    await expect(collection).toHaveAttribute("aria-expanded", "false")
    expect(errors).toEqual([])
  })
}
