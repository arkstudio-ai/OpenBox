import { expect, test } from "@playwright/test"
import { waitForIdleAgent } from "./helpers/agent"

test("refresh restores a live thinking turn and its latest durable state", async ({ page }) => {
  test.setTimeout(180_000)
  await page.goto("/app")
  await waitForIdleAgent(page)

  const composer = page.getByPlaceholder(/输入消息|Message,/)
  await composer.fill(
    "刷新恢复自动化测试：不要调用任何工具。请写三十条详细的实时系统并发检查项，最后输出 REFRESH_RECOVERY_DONE。",
  )
  await composer.press("Enter")
  await expect(page).toHaveURL(/\/app\/s\//, { timeout: 20_000 })
  const stop = page.getByRole("button", { name: /停止|Stop/ }).first()
  await expect(stop).toBeVisible({ timeout: 10_000 })

  // Let several streamed chunks cross a persistence checkpoint. The restored
  // page must not fall back to the user prompt plus an idle composer.
  await page.waitForTimeout(1_500)
  const beforeReloadLength = (await page.locator("main").innerText()).length
  await page.reload()

  await expect(page.getByRole("button", { name: /停止|Stop/ }).first()).toBeVisible({ timeout: 5_000 })
  await expect(page.getByPlaceholder(/运行中|running/i)).toBeVisible()
  await expect
    .poll(async () => (await page.locator("main").innerText()).length, { timeout: 5_000 })
    .toBeGreaterThanOrEqual(beforeReloadLength - 80)

  // Completion while disconnected is covered by the deterministic Bash test
  // below. End this deliberately long prose turn once recovery is proven so
  // model output limits and prose compliance cannot make the infra test flaky.
  await page.getByRole("button", { name: /停止|Stop/ }).first().click()
  await waitForIdleAgent(page, 30_000)
  await expect(page.getByRole("button", { name: /停止|Stop/ })).toHaveCount(0, { timeout: 10_000 })
  await expect(page.getByPlaceholder(/输入消息|Message,/)).toBeVisible()
})

test("closing the page does not cancel the agent and reopening shows completion", async ({ page, context }) => {
  test.setTimeout(180_000)
  await page.goto("/app")
  await waitForIdleAgent(page)

  const composer = page.getByPlaceholder(/输入消息|Message,/)
  await composer.fill(
    "后台执行自动化测试：请使用 bash 执行 python3 -c \"import time; time.sleep(12); print('BACKGROUND_RECOVERY_DONE')\"，等待完成后只回复 BACKGROUND_RECOVERY_DONE。",
  )
  await composer.press("Enter")
  await expect(page).toHaveURL(/\/app\/s\//, { timeout: 20_000 })
  await expect(page.getByRole("button", { name: /停止|Stop/ }).first()).toBeVisible({ timeout: 10_000 })
  const sessionUrl = page.url()

  await page.close()
  await new Promise((resolve) => setTimeout(resolve, 18_000))

  const reopened = await context.newPage()
  await reopened.goto(sessionUrl)
  // The completion marker also appears in the user prompt, and a freshly
  // mounted route has a brief pre-fetch state. Poll the authenticated durable
  // status first, then verify the recovered UI is ready for the next turn.
  await waitForIdleAgent(reopened, 90_000)
  await expect(reopened.getByRole("button", { name: /停止|Stop/ })).toHaveCount(0, {
    timeout: 10_000,
  })
  await expect(reopened.getByText("BACKGROUND_RECOVERY_DONE", { exact: true }).last()).toBeVisible({
    timeout: 10_000,
  })
  await expect(reopened.getByPlaceholder(/输入消息|Message,/)).toBeVisible()
})
