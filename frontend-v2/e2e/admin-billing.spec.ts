import { expect, test, type Page } from "@playwright/test"

async function setup(page: Page) {
  const term = {
    id: "term-1",
    order_id: null,
    source: "admin",
    plan_id: "pro",
    cycle: "monthly",
    starts_at: "2026-01-01T00:00:00Z",
    ends_at: "2030-01-01T00:00:00Z",
    revision: "a".repeat(64),
    cancelled_at: null,
  }
  const detail = {
    can_manage: true,
    workspace: {
      id: "target",
      name: "用户创作空间",
      kind: "personal",
      created_at: "2026-01-01T00:00:00Z",
      is_deleted: false,
      deleted_at: null,
    },
    owner: { id: "target-user", username: "用户 e", email: "e@example.invalid" },
    member_count: 1,
    balance: "12.5",
    plan_id: "pro",
    subscription: term,
    queued: [],
    history: [term],
    ledger: [],
    orders: [],
    operations: [],
    usage: { days: 30, since: "2026-09-01T00:00:00Z", items: [] },
  }
  const writes: { method: string; path: string; body: Record<string, string> }[] = []
  const errors: string[] = []
  page.on("pageerror", (e) => errors.push(e.message))
  await page.route("**/api/**", async (route) => {
    const request = route.request()
    if (!new URL(request.url()).pathname.startsWith("/api/")) return route.continue()
    if (request.method() === "GET") return route.fulfill({ json: detail })
    const body = request.postDataJSON() as Record<string, string>
    writes.push({ method: request.method(), path: new URL(request.url()).pathname, body })
    if (request.url().endsWith("/credits")) detail.balance = "112.623456"
    return route.fulfill({
      json: { operation_id: "receipt", balance: detail.balance, subscription: term, replayed: false },
    })
  })
  await page.goto("/e2e/fixtures/admin-billing.html")
  await expect(page.getByRole("button", { name: "充值积分", exact: true })).toBeVisible()
  return { writes, errors }
}

test("desktop credit confirmation and refreshed balance", async ({ page }) => {
  await page.setViewportSize({ width: 1360, height: 900 })
  const { writes, errors } = await setup(page)
  await page.getByRole("button", { name: "充值积分", exact: true }).click()
  const dialog = page.getByRole("dialog")
  await expect(dialog.getByRole("button", { name: "充值积分", exact: true })).toBeDisabled()
  await dialog.getByLabel("充值积分数", { exact: false }).fill("100.123456")
  await dialog.getByLabel("操作原因（必填）").fill("客服补充积分")
  await dialog.getByRole("checkbox").check()
  await page.screenshot({
    path: "../test-results/local-dev/admin-billing-management/web-credit-dialog.png",
    fullPage: true,
  })
  await dialog.getByRole("button", { name: "充值积分", exact: true }).click()
  await expect(page.getByRole("status")).toBeVisible()
  await expect(page.getByText("112.623456", { exact: true })).toBeVisible()
  expect(writes).toHaveLength(1)
  expect(writes[0].body.credits).toBe("100.123456")
  expect(errors).toEqual([])
})

test("narrow grant form fits and submits the chosen plan", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  const { writes, errors } = await setup(page)
  await page.getByRole("button", { name: "开通 / 续期", exact: true }).click()
  const dialog = page.getByRole("dialog")
  await dialog.getByRole("combobox", { name: "套餐", exact: true }).selectOption("max")
  await dialog.getByRole("combobox", { name: "开通时长", exact: true }).selectOption("yearly")
  await dialog.getByLabel("操作原因（必填）").fill("年度合作订阅")
  await dialog.getByRole("checkbox").check()
  await page.screenshot({
    path: "../test-results/local-dev/admin-billing-management/web-grant-narrow.png",
    fullPage: true,
  })
  const box = await dialog.boundingBox()
  expect(box!.x).toBeGreaterThanOrEqual(0)
  expect(box!.x + box!.width).toBeLessThanOrEqual(390)
  await dialog.getByRole("button", { name: "开通 / 续期", exact: true }).click()
  await expect(page.getByRole("status")).toBeVisible()
  expect(writes[0].body).toMatchObject({ plan_id: "max", cycle: "yearly" })
  expect(errors).toEqual([])
})

test("ending a term is confirmed and bound to its revision", async ({ page }) => {
  const { writes, errors } = await setup(page)
  await page.getByRole("button", { name: "终止订阅", exact: true }).click()
  const dialog = page.getByRole("dialog")
  await dialog.getByLabel("操作原因（必填）").fill("按用户申请终止")
  await dialog.getByRole("checkbox").check()
  await dialog.getByRole("button", { name: "终止订阅", exact: true }).click()
  await expect(page.getByRole("status")).toBeVisible()
  expect(writes[0]).toMatchObject({
    method: "POST",
    path: "/api/admin/billing/workspaces/target/subscriptions/term-1/cancel",
    body: { expected_revision: "a".repeat(64) },
  })
  expect(errors).toEqual([])
})
