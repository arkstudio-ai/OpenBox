import { expect, test } from "@playwright/test"

for (const width of [320, 390, 1280]) {
  test(`context optimization stays in the process across loop and history at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 844 })
    await page.addInitScript(() => localStorage.setItem("bossip:lang", "zh-CN"))
    const errors: string[] = []
    page.on("pageerror", (error) => errors.push(error.message))
    await page.route("**/api/**", (route) => new URL(route.request().url()).pathname.startsWith("/api/")
      ? route.fulfill({ json: {} }) : route.continue())
    await page.goto("/e2e/fixtures/compaction.html")
    const trace = page.locator("[data-compaction-state]")
    await expect(trace).toHaveAttribute("data-compaction-state", "running")
    await expect(trace.getByRole("button", { name: /上下文优化/ })).toHaveAttribute("aria-expanded", "false")
    await expect(page.getByRole("heading", { name: "Goal", exact: true })).toHaveCount(0)
    await page.getByRole("button", { name: "继续执行", exact: true }).click()
    await expect(trace).toHaveAttribute("data-compaction-state", "completed")
    await expect(page.getByText("已继续执行下一步", { exact: true })).toBeVisible()
    await page.getByRole("button", { name: "迟到历史", exact: true }).click()
    await expect(trace).toHaveAttribute("data-compaction-state", "completed")
    await page.getByRole("button", { name: "再次压缩", exact: true }).click()
    await expect(trace).toHaveCount(2)
    await expect(page.getByRole("heading", { name: "Goal", exact: true })).toHaveCount(0)
    await page.screenshot({ path: `test-results/compaction-${width}-collapsed.png`, fullPage: true })
    await trace.first().getByRole("button", { name: /上下文优化/ }).click()
    await expect(page.getByRole("heading", { name: "Goal", exact: true })).toBeVisible()
    await trace.first().getByRole("button", { name: /上下文优化/ }).click()
    for (const [button, state] of [["压缩失败", "failed"], ["压缩中断", "interrupted"]]) {
      await page.getByRole("button", { name: button, exact: true }).click()
      await expect(trace).toHaveAttribute("data-compaction-state", state)
      await expect(page.getByRole("heading", { name: "Goal", exact: true })).toHaveCount(0)
    }
    await page.goto("/e2e/fixtures/compaction.html?state=completed")
    await page.reload()
    await expect(trace).toHaveAttribute("data-compaction-state", "completed")
    await expect(page.getByRole("heading", { name: "Goal", exact: true })).toHaveCount(0)
    await expect(page.getByText("已继续执行下一步", { exact: true })).toBeVisible()
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    expect(errors).toEqual([])
  })
}
