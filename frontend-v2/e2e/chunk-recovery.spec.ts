import { expect, test } from "@playwright/test"

for (const initialStatus of [200, 404]) {
  test(`recovers once from a ${initialStatus} HTML chunk and preserves the URL`, async ({ page }) => {
    let attempts = 0
    let documents = 0
    page.on("request", (r) => {
      if (r.isNavigationRequest()) documents++
    })
    await page.route("**/recovery-target.js*", (route) => {
      attempts++
      return attempts === 1
        ? route.fulfill({
            status: initialStatus,
            contentType: "text/html",
            body: "<!doctype html><p>Old deployment</p>",
          })
        : route.fulfill({
            contentType: "text/javascript",
            body: 'export default function Recovered() { return "授权中心已恢复" }',
          })
    })
    const url = "/e2e/fixtures/chunk-recovery.html?keep=this#details"
    await page.goto(url)
    await page.getByRole("button", { name: "打开授权中心" }).click()
    await expect.poll(() => documents).toBe(2)
    await expect(page).toHaveURL(new RegExp("keep=this#details$"))
    await page.getByRole("button", { name: "打开授权中心" }).click()
    await expect(page.getByText("授权中心已恢复")).toBeVisible()
    expect(attempts).toBe(2)
    expect(documents).toBe(2)
  })
}

test("permanent missing chunks stop automatic refresh and leave a usable manual recovery", async ({
  page,
}) => {
  await page.setViewportSize({ width: 320, height: 700 })
  let documents = 0
  page.on("request", (r) => {
    if (r.isNavigationRequest()) documents++
  })
  await page.route("**/recovery-target.js*", (route) =>
    route.fulfill({ status: 404, contentType: "text/plain", body: "Missing" }),
  )
  await page.goto("/e2e/fixtures/chunk-recovery.html")
  await page.getByRole("button", { name: "打开授权中心" }).click()
  await expect.poll(() => documents).toBe(2)
  await page.getByRole("button", { name: "打开授权中心" }).click()
  await expect(page.getByRole("heading", { name: "页面资源加载失败" })).toBeVisible()
  const reload = page.getByRole("button", { name: "刷新", exact: true })
  await expect(reload).toBeInViewport()
  await expect(page.getByRole("link", { name: "后退" })).toBeInViewport()
  await page.screenshot({ path: "test-results/chunk-recovery-320.png", fullPage: true })
  expect(documents).toBe(2)
  await reload.click()
  await expect.poll(() => documents).toBe(3)
})

test("ordinary errors do not cause an automatic document reload", async ({ page }) => {
  let documents = 0
  page.on("request", (r) => {
    if (r.isNavigationRequest()) documents++
  })
  await page.goto("/e2e/fixtures/chunk-recovery.html?ordinary-error=1")
  await expect(page.getByRole("heading", { name: "出错了" })).toBeVisible()
  expect(documents).toBe(1)
  expect(await page.evaluate(() => sessionStorage.getItem("openbox:chunk-reload-at"))).toBeNull()
})

test("offline failure stays readable and manual reload recovers after reconnect", async ({
  page,
  context,
}) => {
  let documents = 0
  let available = false
  page.on("request", (r) => {
    if (r.isNavigationRequest()) documents++
  })
  await page.route("**/recovery-target.js*", (route) =>
    available
      ? route.fulfill({
          contentType: "text/javascript",
          body: 'export default function Recovered() { return "授权中心已恢复" }',
        })
      : route.abort("internetdisconnected"),
  )
  await page.goto("/e2e/fixtures/chunk-recovery.html")
  await context.setOffline(true)
  await page.getByRole("button", { name: "打开授权中心" }).click()
  await expect(page.getByRole("heading", { name: "页面资源加载失败" })).toBeVisible()
  expect(documents).toBe(1)
  expect(await page.evaluate(() => sessionStorage.getItem("openbox:chunk-reload-at"))).toBeNull()
  available = true
  await context.setOffline(false)
  await page.getByRole("button", { name: "刷新", exact: true }).click()
  await page.getByRole("button", { name: "打开授权中心" }).click()
  await expect(page.getByText("授权中心已恢复")).toBeVisible()
  expect(documents).toBe(2)
})

test("blocked per-tab storage disables automatic refresh, not the recovery page", async ({ page }) => {
  let documents = 0
  page.on("request", (r) => {
    if (r.isNavigationRequest()) documents++
  })
  await page.addInitScript(() =>
    Object.defineProperty(window, "sessionStorage", {
      get() {
        throw new DOMException("Storage blocked", "SecurityError")
      },
    }),
  )
  await page.route("**/recovery-target.js*", (route) =>
    route.fulfill({ status: 404, contentType: "text/plain", body: "Missing" }),
  )
  await page.goto("/e2e/fixtures/chunk-recovery.html")
  await page.getByRole("button", { name: "打开授权中心" }).click()
  await expect(page.getByRole("heading", { name: "页面资源加载失败" })).toBeVisible()
  await expect(page.getByRole("button", { name: "刷新", exact: true })).toBeVisible()
  expect(documents).toBe(1)
})
