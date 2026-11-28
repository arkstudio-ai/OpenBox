import { expect, test, type Page } from "@playwright/test"

const poster = {
  id: "mt_sample",
  title: "春日新品海报",
  entry_id: "poster",
  source: "creator",
  status: "draft",
  version: 1,
  source_session_id: "source",
  recipe: "安排主视觉、标题和品牌区。",
  trials: [] as object[],
}
const form = {
  entry_id: "poster",
  session_id: "trial",
  revision: 1,
  title: poster.title,
  status: "draft",
  shop: "",
  script: "",
  reference_copy: "",
  dishes: [],
  materials: [],
  template: "",
  keep_original: true,
  reference_example_id: poster.id,
  media_template_trial: true,
}
const proof = { mime: "image/jpeg", name: "试做海报", media_url: "/sample-result.jpg" }
const original = { ...proof, name: "原作样片", media_url: "/sample-original.jpg" }

async function setup(page: Page, reviewer = false) {
  const writes: { path: string; data: Record<string, unknown> }[] = []
  const row = { ...poster, status: reviewer ? "pending" : "draft" }
  let task = { ...form }
  let registered = false
  const errors: string[] = []
  page.on("pageerror", (error) => errors.push(error.message))
  await page.route("**/sample-*.jpg", (route) => {
    const name = route.request().url().endsWith("sample-original.jpg")
      ? "gallery-open.jpg"
      : "gallery-genie.jpg"
    const asset = new URL(
      `../../backend/assets/video_templates/gallery-wishes/frames/${name}`,
      import.meta.url,
    )
    return route.fulfill({ contentType: "image/jpeg", path: decodeURIComponent(asset.pathname) })
  })
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url())
    if (!url.pathname.startsWith("/api/")) return route.continue()
    const method = route.request().method()
    const name = url.pathname
    const data = method === "GET" ? {} : (route.request().postDataJSON() ?? {})
    if (method !== "GET") writes.push({ path: name, data })
    const ok = (json: unknown) => route.fulfill({ json })
    if (name.endsWith("/categories")) {
      return ok({
        entries: [
          { id: "video", label: "做一条视频", scenes: [{ id: "story", label: "剧情广告" }] },
          { id: "footage_edit", label: "你拍我剪", scenes: [{ id: "daily", label: "生活纪实" }] },
          { id: "menu", label: "做菜单", scenes: [{ id: "menu_red", label: "烟火红" }] },
          { id: "poster", label: "做海报", scenes: [{ id: "opening", label: "开业宣传" }] },
        ],
      })
    }
    if (name === "/api/media-templates")
      return ok({
        items:
          url.searchParams.get("entry_id") === "poster"
            ? [{ ...poster, id: "mt_public", title: "公开海报模板", status: "listed", ...proof }]
            : [],
      })
    if (name.endsWith("/mine")) return ok({ items: [row] })
    if (name.endsWith("/trial")) {
      if (!registered) row.trials = [{ session_id: "trial", confirmed: false }]
      registered = true
      return ok(task)
    }
    if (name.endsWith("/confirm")) {
      row.trials = [{ session_id: "trial", confirmed: true, output_asset_id: "produced" }]
      return ok({ confirmed: true })
    }
    if (name.endsWith("/submit")) {
      row.status = "pending"
      return ok(row)
    }
    if (name === "/api/light/productions/trial" && method === "PATCH") {
      task = { ...task, ...data, revision: 2 }
      return ok(task)
    }
    if (name === "/api/light/productions/trial/actions") {
      task = { ...task, status: "completed" }
      return ok({ ...task, output: { id: "produced", mime: "image/png", name: "试做海报" } })
    }
    if (name === "/api/light/productions/trial") return ok(task)
    if (name === "/api/assets/produced/url") return ok({ url: proof.media_url, mime: proof.mime })
    if (name === "/api/media-templates/admin")
      return ok({
        items: !url.searchParams.get("status") || row.status === url.searchParams.get("status") ? [row] : [],
        has_more: false,
      })
    if (name === "/api/media-templates/admin/mt_sample")
      return ok({
        ...row,
        original,
        input: { shop: "新的店名", poster_title: "春日限定", dishes: [], keep_original: true, notes: "" },
        materials: [original],
        trial_result: proof,
        decisions: [],
      })
    if (name.endsWith("/decision")) {
      row.status = data.decision
      return ok(row)
    }
    return ok({ items: [] })
  })
  await page.goto(`/e2e/fixtures/media-templates.html${reviewer ? "?admin" : ""}`)
  return { writes, errors }
}

test("template catalogue and private guided publication", async ({ page }, info) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  const { writes, errors } = await setup(page)
  await expect(page.getByRole("button", { name: "应用技能", exact: true })).toBeVisible()
  await expect(page.getByRole("button", { name: "媒体制作技能", exact: true })).toBeVisible()
  await expect(page.getByRole("button", { name: "你拍我剪", exact: true })).toBeVisible()
  await page.getByRole("button", { name: "做海报", exact: true }).click()
  await page.getByRole("combobox", { name: "二级分类" }).selectOption("opening")
  await expect(page.getByText("公开海报模板", { exact: false })).toBeVisible()
  await page.getByRole("button", { name: "预览样片 公开海报模板" }).click()
  await expect(page.getByRole("dialog")).toBeVisible()
  await page.keyboard.press("Escape")
  await page.getByRole("button", { name: "我的模板", exact: true }).click()
  await expect(page.getByRole("button", { name: "提交审核" })).toHaveCount(0)
  await page.getByRole("button", { name: "到精简版试做" }).click()
  await page.getByLabel("店名或品牌").fill("新的店名")
  await page.getByLabel("海报标题").fill("春日限定")
  await page.getByRole("button", { name: "开始制作" }).click()
  await expect(page.getByRole("img", { name: "试做海报" })).toBeVisible()
  await page.getByRole("button", { name: "确认试做成品" }).click()
  await page.getByRole("button", { name: "提交审核" }).click()
  const submit = page.getByRole("dialog", { name: "提交审核" })
  await expect(submit.getByRole("button", { name: "提交审核" })).toBeDisabled()
  await submit.getByLabel("二级分类").selectOption("opening")
  await submit.getByRole("checkbox").check()
  await submit.getByRole("button", { name: "提交审核" }).click()
  await expect(page.getByText("待审核", { exact: true })).toBeVisible()
  expect(writes.find((item) => item.path.endsWith("/submit"))?.data).toEqual({
    session_id: "trial",
    scene: "opening",
    share_finished_preview: true,
  })
  expect(errors).toEqual([])
  await page.screenshot({ path: info.outputPath("author.png"), fullPage: true })
})

test("admin compares the original, inputs and result before a recorded decision", async ({ page }, info) => {
  await page.setViewportSize({ width: 1280, height: 880 })
  const { writes, errors } = await setup(page, true)
  await page.getByRole("combobox", { name: "状态", exact: true }).selectOption("pending")
  await page.getByRole("button", { name: "审核", exact: true }).click()
  const dialog = page.getByRole("dialog", { name: "审核" })
  for (const name of ["原作样片", "试做输入", "试做成品"]) {
    await expect(dialog.getByRole("heading", { name })).toBeVisible()
  }
  await expect(dialog.getByText("新的店名")).toBeVisible()
  await expect(dialog.getByText("春日限定")).toBeVisible()
  await expect(dialog.getByText("保留实拍原声", { exact: true })).toHaveCount(0)
  await expect(dialog.getByText("菜品与价格", { exact: true })).toHaveCount(0)
  await page.screenshot({ path: info.outputPath("review.png"), fullPage: true })
  await dialog.getByRole("button", { name: "驳回", exact: true }).click()
  await expect(dialog.getByRole("button", { name: "确认驳回" })).toBeDisabled()
  await dialog.getByLabel("审核说明").fill("请核对试做价格")
  await dialog.getByRole("button", { name: "确认驳回" }).click()
  expect(writes.find((item) => item.path.endsWith("/decision"))?.data).toEqual({
    decision: "rejected",
    reason: "请核对试做价格",
  })
  expect(errors).toEqual([])
})

test("admin filters and category management align and wrap with the content pane", async ({ page }) => {
  await page.setViewportSize({ width: 1024, height: 768 })
  await setup(page, true)
  const fields = [
    page.getByRole("combobox", { name: "制作类型", exact: true }),
    page.getByRole("combobox", { name: "二级分类", exact: true }),
    page.getByRole("combobox", { name: "状态", exact: true }),
    page.getByRole("searchbox", { name: "搜索模板", exact: true }),
    page.getByRole("button", { name: "管理分类", exact: true }),
  ]
  await expect(fields[4]).toBeVisible()
  const boxes = () =>
    Promise.all(fields.map(async (field) => {
      const box = await field.boundingBox()
      if (!box) throw new Error("The filter control is missing.")
      return box
    }))
  const desktop = await boxes()
  for (const box of desktop) {
    expect(box.y).toBeCloseTo(desktop[0].y)
    expect(box.height).toBeCloseTo(desktop[0].height)
  }
  expect(desktop[3].width).toBeGreaterThan(desktop[0].width)

  await page.setViewportSize({ width: 660, height: 768 })
  const compact = await boxes()
  expect(compact[0].y).toBeCloseTo(compact[1].y)
  expect(compact[2].y).toBeCloseTo(compact[3].y)
  expect(compact[2].y).toBeGreaterThan(compact[0].y)
  expect(compact[4].y).toBeGreaterThan(compact[3].y)

  await page.setViewportSize({ width: 390, height: 768 })
  const narrow = await boxes()
  expect(narrow[1].y).toBeGreaterThan(narrow[0].y)
  for (const box of narrow) {
    expect(box.x + box.width).toBeLessThanOrEqual(390)
  }
})
