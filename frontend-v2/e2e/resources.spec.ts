import { expect, test } from "@playwright/test"

// The resource centre is a view over the OSS ledger, and the composer's "@"
// menu is a second view over the same data. These specs cover what a unit test
// cannot: the real /api/assets contract, and the hand-off between the
// resources feature and the chat composer.

test("resource centre filters by project and then by source", async ({ page }) => {
  // Read the project list off the page's own request — the access token lives
  // in memory, so a bare APIRequestContext call would come back 401.
  const projectsResp = page.waitForResponse(
    (r) => r.url().includes("/api/agent/project") && r.request().method() === "GET",
  )
  const listed = page.waitForResponse(
    (r) => r.url().includes("/api/assets?") && r.request().method() === "GET",
  )
  await page.goto("/app/resources")
  await listed

  // The listing rendered (the storage footer that used to stand in for this
  // was removed in F.8).
  await expect(page.getByRole("navigation")).toBeVisible()

  const projects = (await (await projectsResp).json()) as { id: string; name: string }[]
  expect(projects.length).toBeGreaterThan(0)
  const target = projects[0]

  const rail = page.getByRole("navigation")
  const scoped = page.waitForResponse((r) => r.url().includes(`project=${target.id}`))
  await rail.getByText(target.name, { exact: true }).click()
  await scoped
  await expect(page).toHaveURL(new RegExp(`project=${target.id}`))

  // The second level — user input vs model output — only exists under a
  // selected project, which is the whole point of the nesting.
  const bySource = page.waitForResponse((r) => r.url().includes("source=agent"))
  await rail.getByText(/^模型产出$|^Model output$/).click()
  const rows = (await (await bySource).json()) as { items: { source: string }[] }
  for (const item of rows.items) expect(item.source).toBe("agent")
  await expect(page).toHaveURL(/source=agent/)
})

test("@ in the composer attaches a resource without typing a path", async ({ page }) => {
  await page.goto("/app")

  const composer = page.getByPlaceholder(/输入消息|Message,/)
  await composer.click()
  await composer.pressSequentially("@")

  // The menu opens scoped to a project, with the source switcher beside it.
  const menu = page.getByRole("listbox")
  await expect(menu).toBeVisible()
  await expect(page.getByRole("button", { name: /^全部$|^Any$/ })).toBeVisible()

  const first = menu.getByRole("option").first()
  await expect(first).toBeVisible({ timeout: 10_000 })
  await first.click()

  // Picking attaches the file and clears the trigger — nothing is typed.
  await expect(composer).toHaveValue("")
  await expect(page.locator("[title*='·']").first()).toBeVisible()
})

test("the + menu opens the same resource menu", async ({ page }) => {
  await page.goto("/app")
  await page.getByTestId("composer-tools").click()
  await page.getByRole("menuitem", { name: /资源中心|Resources/ }).click()

  await expect(page.getByRole("listbox")).toBeVisible()
  await expect(page.getByPlaceholder(/输入消息|Message,/)).toHaveValue("@")
})

test("the list column can be dragged wider, and stays wide", async ({ page }) => {
  await page.goto("/app/resources")

  const resizer = page.getByTitle(/拖动调整列表宽度|Drag to resize the list/)
  await expect(resizer).toBeAttached()
  // The handle sits on the column's own border, so its parent IS the column.
  const column = resizer.locator("..")
  const before = (await column.boundingBox())!.width

  const box = (await resizer.boundingBox())!
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2)
  await page.mouse.down()
  await page.mouse.move(box.x + box.width / 2 + 90, box.y + box.height / 2, { steps: 8 })
  await page.mouse.up()

  const after = (await column.boundingBox())!.width
  expect(after).toBeGreaterThan(before + 60)
  // Dragging must not leave the page in a text-unselectable state.
  expect(await page.evaluate(() => document.body.style.userSelect)).toBe("")

  // The width someone dragged is theirs — it survives a reload.
  await page.reload()
  const reloaded = (await column.boundingBox())!.width
  expect(Math.round(reloaded)).toBe(Math.round(after))
})
