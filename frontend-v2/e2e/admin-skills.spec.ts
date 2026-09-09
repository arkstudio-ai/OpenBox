import { expect, test, type Page, type Route } from "@playwright/test"

function jsonBody(route: Route, path: string): Record<string, unknown> {
  return route.request().method() === "GET" || path === "/store/upload"
    ? {}
    : (route.request().postDataJSON() ?? {})
}

function entry(name: string) {
  return {
    catalog_id: `skill:${name}`,
    name,
    kind: "skill",
    title: name,
    description: "Test package",
    icon: "🧪",
    origin: "official",
    publisher: "OpenBox",
    listing: "listed",
    is_official: true,
    featured: false,
    installs_count: 0,
    revision: 1,
    content: `---\nname: ${name}\ndescription: Test package\n---\n\nTest instructions.\n`,
    deleted: false,
  }
}
type Entry = ReturnType<typeof entry>
async function fixture(
  page: Page,
  options: {
    editConflict?: boolean
    deletePartial?: boolean
    offline?: boolean
    partialScan?: boolean
    uploadNetwork?: boolean
    uninstallFailed?: boolean
  } = {},
) {
  const entries = [entry("alpha"), entry("beta")]
  const writes: { path: string; body: Record<string, unknown> }[] = []
  const scans: string[] = []
  const errors: string[] = []
  const desktop = {
    desktop_id: "desktop-fixture",
    workspace_name: "测试空间",
    status: "running",
    channel_state: "up",
    members: [
      { id: "alice", username: "Alice", email: "alice@example.test" },
      { id: "bob", username: "Bob", email: "bob@example.test" },
    ],
  }
  let live = [
    {
      kind: "skill",
      name: "manual-install",
      install_dir: "manual-install",
      source: "container",
      description: "Manually installed",
      removable: true,
    },
    {
      kind: "skill",
      name: "system-skill",
      install_dir: "system-skill",
      source: "builtin",
      description: "System",
      removable: false,
    },
  ]
  let uploads = 0
  page.on("pageerror", (error) => errors.push(error.message))
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url())
    if (!url.pathname.startsWith("/api/")) return route.continue()
    const path = decodeURIComponent(url.pathname).replace("/api/admin/skills", "")
    const method = route.request().method()
    const body = jsonBody(route, path)
    if (method !== "GET") writes.push({ path, body })
    if (path === "/store" && method === "GET") {
      const items = entries.filter((e) => e.deleted === (url.searchParams.get("deleted") === "true"))
      return route.fulfill({ json: { items, total: items.length, offset: 0, limit: 20 } })
    }
    if (path === "/store" && method === "POST") {
      const created = { ...entry(String(body.name)), ...body, listing: "delisted" } as Entry
      entries.push(created)
      return route.fulfill({ json: { catalog_id: created.catalog_id, revision: 1 } })
    }
    if (path === "/store/upload") {
      uploads += 1
      if (options.uploadNetwork) return route.abort("internetdisconnected")
      const names = [
        ...(route.request().postDataBuffer()?.toString() ?? "").matchAll(/filename="([^"]+)"/g),
      ].map((m) => m[1])
      return route.fulfill({
        json: {
          items: names.map((name) => ({
            filename: name,
            ok: name !== "bad.zip" || uploads > 1,
            error: "Invalid ZIP",
          })),
        },
      })
    }
    if (path === "/store/batch-delete") {
      const items = (body.catalog_ids as string[]).map((id) => {
        const ok = !(options.deletePartial && id === "skill:beta")
        const found = entries.find((e) => e.catalog_id === id)
        if (ok && found) found.deleted = true
        return { catalog_id: id, ok, error: ok ? undefined : "conflict" }
      })
      return route.fulfill({ json: { items } })
    }
    if (path.startsWith("/store/")) {
      const id = path.split("/")[2]
      const found = entries.find((e) => e.catalog_id === id)
      if (path.endsWith("/restore") && found) {
        found.deleted = false
        found.listing = "delisted"
      }
      if (method === "PATCH" && found) {
        if (options.editConflict)
          return route.fulfill({ status: 409, json: { detail: "Entry changed; refresh before saving" } })
        Object.assign(found, body, { revision: found.revision + 1 })
      }
      return route.fulfill({ json: found ?? {} })
    }
    if (path === "/desktops")
      return route.fulfill({ json: { items: [desktop], total: 1, offset: 0, limit: 20 } })
    if (path.endsWith("/skills")) {
      scans.push(url.searchParams.get("user_id")!)
      if (options.offline) return route.fulfill({ status: 503, json: { detail: "Desktop unavailable" } })
      return route.fulfill({
        json: {
          items: live,
          desktop_id: desktop.desktop_id,
          user_id: url.searchParams.get("user_id"),
          unavailable: options.partialScan ? ["mcp"] : [],
          scanned_at: "2026-09-09T05:00:00Z",
        },
      })
    }
    if (path.endsWith("/uninstall")) {
      if (options.uninstallFailed)
        return route.fulfill({ status: 504, json: { detail: "Uninstall uncertain" } })
      live = live.filter((item) => item.install_dir !== body.install_dir)
      return route.fulfill({ json: { ok: true } })
    }
    // Never forward a request to the developer's or production backend.
    return route.fulfill({ json: { items: [], total: 0, offset: 0, limit: 20 } })
  })
  await page.goto("/e2e/fixtures/admin-skills.html")
  await expect(page.getByText("alpha", { exact: true })).toBeVisible()
  return { writes, scans, errors }
}

for (const width of [320, 390, 768, 1440]) {
  test(`CRUD, icon and soft-delete recovery at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 })
    const state = await fixture(page)
    const alpha = page.getByRole("row").filter({ has: page.getByText("alpha", { exact: true }) })
    await expect(alpha.getByRole("button", { name: "编辑", exact: true })).toBeVisible()
    await expect(alpha.getByRole("button", { name: "删除", exact: true })).toBeVisible()
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
    expect(
      await page.getByRole("table").evaluate((table) => {
        const container = table.parentElement!
        return container.scrollWidth <= container.clientWidth
      }),
    ).toBe(true)
    await expect(alpha.getByText("已上架", { exact: true }).filter({ visible: true })).toBeVisible()
    await page.screenshot({ path: testInfo.outputPath(`store-${width}.png`), fullPage: true })
    await alpha.getByRole("button", { name: "编辑", exact: true }).click()
    const dialog = page.getByRole("dialog", { name: "编辑", exact: true })
    await dialog.getByLabel("展示名称", { exact: true }).fill("Updated alpha")
    await dialog.getByLabel("展示图标", { exact: false }).fill("🚀")
    await dialog.getByRole("button", { name: "保存", exact: true }).click()
    await expect(dialog).toHaveCount(0)
    await expect(page.getByText("Updated alpha", { exact: true })).toBeVisible()
    const edit = state.writes.find((w) => w.path === "/store/skill:alpha")!
    expect(edit.body.expected_revision).toBe(1)
    expect(edit.body.icon).toBe("🚀")
    expect(edit.body.content).toBeUndefined()
    await page.getByRole("button", { name: "新增技能 / MCP", exact: true }).click()
    await page.getByLabel("固定标识", { exact: false }).fill("gamma")
    await page.getByLabel("展示名称", { exact: true }).fill("Gamma")
    await page.getByLabel("SKILL.md 内容", { exact: true }).fill(entry("gamma").content)
    await page.getByRole("button", { name: "保存", exact: true }).click()
    await expect(page.getByText("Gamma", { exact: true })).toBeVisible()
    await page.getByRole("checkbox", { name: "选择 Gamma", exact: true }).check()
    await page.getByRole("button", { name: "删除所选（1）", exact: true }).click()
    const remove = page.getByRole("dialog")
    await expect(remove.getByRole("button", { name: "删除", exact: true })).toBeDisabled()
    await remove.getByRole("textbox").fill("Regression cleanup")
    await remove.getByRole("button", { name: "删除", exact: true }).click()
    await expect(page.getByText("Gamma", { exact: true })).toHaveCount(0)
    await page.getByRole("button", { name: "回收站", exact: true }).click()
    await expect(page.getByText("Gamma", { exact: true })).toBeVisible()
    await page.getByRole("button", { name: "恢复", exact: true }).click()
    await expect(page.getByText("已恢复为下架状态，可检查后重新上架。")).toBeVisible()
    expect(state.errors).toEqual([])
  })
}

test("multi-select ZIPs; retry only failures and reject duplicates in queue", async ({ page }) => {
  await fixture(page)
  await page.getByRole("button", { name: "批量上传 ZIP", exact: true }).click()
  const input = page.getByLabel("选择多个压缩包", { exact: true })
  await expect(input).toHaveAttribute("multiple", "")
  const files = ["good.zip", "bad.zip"].map((name) => ({
    name,
    mimeType: "application/zip",
    buffer: Buffer.from("test"),
  }))
  await input.setInputFiles(files)
  await page.getByRole("button", { name: "上传 / 重试 2 个文件", exact: true }).click()
  await expect(page.getByText("上传成功", { exact: true })).toHaveCount(1)
  await expect(page.getByText("Invalid ZIP", { exact: true })).toBeVisible()
  await page.getByRole("button", { name: "上传 / 重试 1 个文件", exact: true }).click()
  await expect(page.getByText("上传成功", { exact: true })).toHaveCount(2)
  await expect(page.getByRole("button", { name: "上传 / 重试 0 个文件", exact: true })).toBeDisabled()
})

test("upload transport loss stays uncertain and retryable", async ({ page }) => {
  await fixture(page, { uploadNetwork: true })
  await page.getByRole("button", { name: "批量上传 ZIP", exact: true }).click()
  await page
    .getByLabel("选择多个压缩包", { exact: true })
    .setInputFiles({ name: "one.zip", mimeType: "application/zip", buffer: Buffer.from("test") })
  await page.getByRole("button", { name: "上传 / 重试 1 个文件", exact: true }).click()
  await expect(page.getByText(/未能确认上传结果/)).toBeVisible()
  await expect(page.getByRole("button", { name: "上传 / 重试 1 个文件", exact: true })).toBeEnabled()
})

test("stale edit is not silently overwritten", async ({ page }) => {
  await fixture(page, { editConflict: true })
  await page.getByRole("button", { name: "编辑", exact: true }).first().click()
  await page.getByLabel("展示名称", { exact: true }).fill("Unsaved draft")
  await page.getByRole("button", { name: "保存", exact: true }).click()
  await expect(page.getByRole("alert")).toHaveText("Entry changed; refresh before saving")
  await expect(page.getByLabel("展示名称", { exact: true })).toHaveValue("Unsaved draft")
})

test("partial batch delete retains failed selection", async ({ page }) => {
  await fixture(page, { deletePartial: true })
  await page.getByRole("button", { name: "全选 / 取消本页", exact: true }).click()
  await page.getByRole("button", { name: "删除所选（2）", exact: true }).click()
  await page.getByRole("dialog").getByRole("textbox").fill("Test")
  await page.getByRole("dialog").getByRole("button", { name: "删除", exact: true }).click()
  await expect(page.getByRole("status")).toContainText("已删除 1 项，失败 1 项")
  await expect(page.getByRole("checkbox", { name: "选择 beta", exact: true })).toBeChecked()
})

async function openDesktop(page: Page) {
  await page.getByRole("link", { name: "用户安装", exact: true }).click()
  await page.getByRole("button", { name: /测试空间/ }).click()
  await page.getByRole("button", { name: "扫描 / 刷新实际安装", exact: true }).click()
}

test("scans manual installations, protects builtins and scopes uninstall", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 390, height: 900 })
  const state = await fixture(page)
  await openDesktop(page)
  await expect(page.getByText("manual-install", { exact: true })).toBeVisible()
  await expect(page.getByRole("button", { name: "系统项 / 不可卸载", exact: true })).toBeDisabled()
  expect(state.scans).toEqual(["alice"])
  await page.screenshot({ path: testInfo.outputPath("desktop-mobile.png"), fullPage: true })
  await page.getByRole("button", { name: "卸载", exact: true }).click()
  const dialog = page.getByRole("dialog")
  await expect(dialog).toContainText("desktop-fixture")
  await expect(dialog).toContainText("alice")
  await expect(dialog.getByRole("button", { name: "卸载", exact: true })).toBeDisabled()
  await dialog.getByRole("textbox").fill("Remove test copy")
  await dialog.getByRole("button", { name: "卸载", exact: true }).click()
  await expect(page.getByText("manual-install", { exact: true })).toHaveCount(0)
  expect(state.writes.find((w) => w.path.endsWith("/uninstall"))?.body).toEqual({
    user_id: "alice",
    kind: "skill",
    install_dir: "manual-install",
    reason: "Remove test copy",
  })
  await page.getByRole("combobox").selectOption("bob")
  await expect(page.getByText("system-skill", { exact: true })).toHaveCount(0)
  await page.getByRole("button", { name: "扫描 / 刷新实际安装", exact: true }).click()
  await expect(page.getByText("system-skill", { exact: true })).toBeVisible()
  expect(state.scans.at(-1)).toBe("bob")
  expect(state.errors).toEqual([])
})

test("offline scan is unknown, never an empty installation list", async ({ page }) => {
  await fixture(page, { offline: true })
  await openDesktop(page)
  await expect(page.getByRole("alert")).toContainText("无法确认实际安装状态")
  await expect(page.getByText(/此用户目录内没有/)).toHaveCount(0)
})

test("partial scan labels missing kinds", async ({ page }) => {
  await fixture(page, { partialScan: true })
  await openDesktop(page)
  await expect(page.getByRole("alert")).toContainText("部分扫描失败（mcp）")
  await expect(page.getByText("manual-install", { exact: true })).toBeVisible()
})

test("failed uninstall keeps confirmation and does not claim success", async ({ page }) => {
  await fixture(page, { uninstallFailed: true })
  await openDesktop(page)
  await page.getByRole("button", { name: "卸载", exact: true }).click()
  await page.getByRole("dialog").getByRole("textbox").fill("Test")
  await page.getByRole("dialog").getByRole("button", { name: "卸载", exact: true }).click()
  await expect(page.getByRole("dialog").getByRole("alert")).toBeVisible()
  await expect(page.getByText(/已卸载，并已回读确认/)).toHaveCount(0)
})
