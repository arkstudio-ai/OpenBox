import { test, expect } from "@playwright/test"

for (const width of [320, 390, 1280]) {
  for (const dark of [false, true]) {
    test(`notification diagnostics width=${width} dark=${dark}`, async ({ page }) => {
      await page.setViewportSize({ width, height: 844 })
      const sent: Record<string, string>[] = []
      await page.route("**/api/admin/push**", async (route) => {
        if (route.request().method() === "POST") {
          sent.push(route.request().postDataJSON() as Record<string, string>)
          return route.fulfill({ status: 202, json: {} })
        }
        return route.fulfill({ json: {
          device: { registered: true, bindingId: "fixture-phone", platform: "ios", notificationsEnabled: true, ready: true },
          presence: { appState: "background" }, providers: [{ id: "apns", configured: true }, { id: "jpush", configured: true }],
          templates: [{ id: "system_test", title: "通知测试", body: "手机通知正常。" }], tests: [], delaySeconds: 10, cooldownSeconds: 30,
        } })
      })
      await page.goto("/e2e/fixtures/admin-notifications.html")
      await page.evaluate((dark) => { document.documentElement.dataset.mode = dark ? "dark" : "light" }, dark)
      const button = page.getByRole("button", { name: "发送远程测试" })
      await expect(button).toBeEnabled()
      await button.click()
      await expect(page.getByRole("status")).toBeVisible()
      expect(sent).toEqual([{ template: "system_test", bindingId: "fixture-phone", requestId: expect.any(String) }])
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
      await page.screenshot({ path: `/private/tmp/openbox-admin-push-web-${width}-${dark ? "dark" : "light"}.png`, fullPage: true })
    })
  }
}
