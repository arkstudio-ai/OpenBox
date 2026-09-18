import { expect, test, type Page } from "@playwright/test"
import type { QuestionRequest } from "../src/shared/types/api"

const initialQuestion: QuestionRequest = {
  id: "fixture-ask",
  user_id: "fixture-user",
  session_id: "fixture-session",
  status: "pending",
  questions: [
    { question: "请选择时长", options: [{ label: "30秒" }, { label: "60秒" }] },
    { question: "请选择字幕", options: [{ label: "保留" }, { label: "不保留" }] },
    { question: "请选择语气", options: [{ label: "轻松" }, { label: "正式" }] },
  ],
  draft_revision: 0,
  draft: Array.from({ length: 3 }, () => ({ selected: [] as string[], custom: "", use_custom: false })),
}

async function fixture(
  page: Page,
  options: {
    firstAnswerStatus?: number | "network"
    delayList?: Promise<void>
    holdAnswer?: Promise<void>
    questions?: QuestionRequest["questions"]
  } = {},
) {
  let question = structuredClone({ ...initialQuestion, questions: options.questions ?? initialQuestion.questions })
  let pending = !options.delayList
  const answers: unknown[] = []
  const rejections: string[] = []
  const errors: string[] = []
  page.on("pageerror", (error) => errors.push(error.message))
  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname
    if (!path.startsWith("/api/")) return route.continue()
    const method = route.request().method()
    if (path === "/api/agent/config")
      return route.fulfill({
        json: {
          models: [
            {
              id: "openai/test",
              name: "Extremely long chat model name for a narrow screen",
              variants: ["low", "high"],
            },
          ],
          default_model: "openai/test",
          video_models: [{ id: "video-test", name: "Long video model name", resolutions: ["1080p"] }],
          default_video_model: "video-test",
          default_video_resolution: "1080p",
        },
      })
    if (path === "/api/containers") return route.fulfill({ json: { containers: [] } })
    if (path === "/api/auth/me/preferences") return route.fulfill({ json: { extra: {} } })
    if (path === "/api/agent/question") {
      const snapshot = pending ? [structuredClone(question)] : []
      await options.delayList
      return route.fulfill({ json: snapshot })
    }
    if (path === "/api/agent/question/fixture-ask/draft" && method === "PUT") {
      const body = route.request().postDataJSON()
      question = { ...question, draft: body.draft, draft_revision: body.revision + 1 }
      return route.fulfill({ json: question })
    }
    if (path === "/api/agent/question/fixture-ask" && method === "GET")
      return route.fulfill({ json: question })
    if (path === "/api/agent/question/fixture-ask/reject" && method === "POST") {
      rejections.push("fixture-ask")
      pending = false
      return route.fulfill({ json: { ok: true, session_id: "fixture-session" } })
    }
    if (path === "/api/agent/question/fixture-ask" && method === "POST") {
      answers.push(route.request().postDataJSON())
      await options.holdAnswer
      const status = answers.length === 1 ? (options.firstAnswerStatus ?? 200) : 200
      if (status === "network") return route.abort("internetdisconnected")
      if (status === 200 || status === 410) pending = false
      return route.fulfill({
        status,
        json:
          status === 200 ? { ok: true, session_id: "fixture-session" } : { detail: { code: "TEST_FAILURE" } },
      })
    }
    // Do not send any unhandled request to a real service.
    return route.fulfill({ json: [] })
  })
  await page.goto("/e2e/fixtures/durable-question.html")
  return { answers, rejections, errors }
}

async function fillAll(page: Page) {
  await expect(page.getByRole("button", { name: "确认", exact: true })).toHaveCount(0)
  await expect(page.getByTestId("question-primary-action")).toHaveText("下一题")
  await expect(page.getByTestId("question-primary-action")).toBeDisabled()
  await expect(page.getByText("1/3", { exact: true })).toBeVisible()
  await expect(page.getByRole("textbox", { name: "请选择字幕", exact: true })).toHaveCount(0)
  await page.getByRole("button", { name: "30秒", exact: true }).click()
  await expect(page.getByText("2/3", { exact: true })).toBeVisible()
  await page.getByRole("button", { name: "保留", exact: true }).click()
  await expect(page.getByText("3/3", { exact: true })).toBeVisible()
  await expect(page.getByRole("button", { name: "确认", exact: true })).toBeDisabled()
  await page.getByRole("textbox", { name: "请选择语气", exact: true }).fill("自然轻松")
}

for (const width of [320, 390, 1280]) {
  test(`all ask and send/stop controls stay inside a ${width}px viewport`, async ({ page }) => {
    await page.setViewportSize({ width, height: 844 })
    const state = await fixture(page)
    await fillAll(page)
    await expect(page.getByRole("button", { name: "确认", exact: true })).toBeEnabled()
    await expect(page.getByRole("button", { name: "确认", exact: true })).toBeInViewport()
    await expect(page.getByRole("button", { name: "上一题", exact: true })).toBeInViewport()
    await expect(page.getByRole("button", { name: "下一题", exact: true })).toBeDisabled()
    const send = page.getByRole("button", { name: "发送", exact: true })
    await expect(send).toBeInViewport()
    const bounds = await send.boundingBox()
    expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(width)
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    await page.getByRole("button", { name: "模拟运行", exact: true }).click()
    const stop = page.getByRole("button", { name: "停止生成", exact: true })
    await expect(stop).toBeInViewport()
    await stop.click()
    await expect(send).toBeVisible()
    expect(state.errors).toEqual([])
  })

  test(`primary Next and Confirm support multiselect at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 844 })
    const state = await fixture(page, {
      questions: [
        initialQuestion.questions[0],
        { question: "请选择效果", multiple: true, options: [{ label: "字幕" }, { label: "配乐" }] },
        { question: "请选择平台", multiple: true, options: [{ label: "抖音" }, { label: "小红书" }] },
      ],
    })
    const primary = page.getByTestId("question-primary-action")
    await page.getByRole("button", { name: "30秒", exact: true }).click()
    await expect(primary).toHaveText("下一题")
    await expect(primary).toBeDisabled()
    await page.getByRole("button", { name: "字幕", exact: true }).click()
    await page.getByRole("button", { name: "配乐", exact: true }).click()
    await expect(page.getByText("2/3", { exact: true })).toBeVisible()
    await expect(primary).toBeEnabled()
    await expect(primary).toBeInViewport()
    await page.getByRole("button", { name: "字幕", exact: true }).click()
    await page.getByRole("button", { name: "配乐", exact: true }).click()
    await expect(primary).toBeDisabled()
    await page.getByRole("button", { name: "字幕", exact: true }).click()
    await page.getByRole("button", { name: "配乐", exact: true }).click()
    await primary.click()
    await expect(page.getByText("3/3", { exact: true })).toBeVisible()
    await expect(primary).toHaveText("确认")
    await expect(primary).toBeDisabled()
    await page.getByRole("button", { name: "抖音", exact: true }).click()
    await page.getByRole("button", { name: "小红书", exact: true }).click()
    await expect(primary).toBeEnabled()
    await expect(primary).toBeInViewport()
    await page.getByRole("button", { name: "上一题", exact: true }).click()
    await expect(primary).toHaveText("下一题")
    await expect(page.getByRole("button", { name: "字幕", exact: true })).toHaveAttribute("aria-pressed", "true")
    await expect(page.getByRole("button", { name: "配乐", exact: true })).toHaveAttribute("aria-pressed", "true")
    await primary.click()
    expect(state.answers).toEqual([])
    await primary.click()
    await expect(primary).toHaveCount(0)
    expect(state.answers).toEqual([{ answers: [["30秒"], ["字幕", "配乐"], ["抖音", "小红书"]] }])
    expect(state.errors).toEqual([])
  })
}

for (const status of [500, 409, 422, "network"] as const) {
  test(`HTTP ${status} keeps answers editable and a retry can finish`, async ({ page }) => {
    const state = await fixture(page, { firstAnswerStatus: status })
    await fillAll(page)
    const confirm = page.getByRole("button", { name: "确认", exact: true })
    await confirm.click()
    await expect.poll(() => state.answers.length).toBe(1)
    await expect(confirm).toBeEnabled()
    await expect(page.getByRole("textbox", { name: "请选择语气", exact: true })).toHaveValue("自然轻松")
    await confirm.click()
    await expect(confirm).toHaveCount(0)
    expect(state.answers).toEqual(Array(2).fill({ answers: [["30秒"], ["保留"], ["自然轻松"]] }))
    expect(state.errors).toEqual([])
  })
}

test("410 removes the stale card instead of offering a misleading retry", async ({ page }) => {
  await fixture(page, { firstAnswerStatus: 410 })
  await fillAll(page)
  await page.getByRole("button", { name: "确认", exact: true }).click()
  await expect(page.getByRole("button", { name: "确认", exact: true })).toHaveCount(0)
})

test("an in-flight answer disables every control and sends only once", async ({ page }) => {
  let release!: () => void
  const state = await fixture(page, {
    holdAnswer: new Promise<void>((resolve) => {
      release = resolve
    }),
  })
  await fillAll(page)
  await page.getByRole("button", { name: "确认", exact: true }).click()
  await expect(page.getByRole("button", { name: "轻松", exact: true })).toBeDisabled()
  await expect(page.getByRole("button", { name: "上一题", exact: true })).toBeDisabled()
  await expect(page.getByRole("button", { name: "下一题", exact: true })).toBeDisabled()
  await expect(page.getByRole("button", { name: "跳过全部", exact: true })).toBeDisabled()
  await expect(page.getByRole("textbox", { name: "请选择语气", exact: true })).toBeDisabled()
  await expect.poll(() => state.answers.length).toBe(1)
  release()
  await expect(page.getByRole("button", { name: "确认", exact: true })).toHaveCount(0)
  expect(state.answers).toHaveLength(1)
})

test("a WS ask arriving during a stale pending read remains answerable", async ({ page }) => {
  let release!: () => void
  await fixture(page, {
    delayList: new Promise<void>((resolve) => {
      release = resolve
    }),
  })
  await expect(page.getByRole("status")).toHaveText("读取中")
  await page.getByRole("button", { name: "模拟新 ask", exact: true }).click()
  release()
  await expect(page.getByRole("status")).toHaveText("读取完成")
  await fillAll(page)
  await expect(page.getByRole("button", { name: "确认", exact: true })).toBeEnabled()
})

test("saved mixed-answer drafts survive a complete page reload", async ({ page }) => {
  await fixture(page)
  await fillAll(page)
  await page.waitForResponse(
    (response) => response.url().endsWith("/fixture-ask/draft") && response.status() === 200,
  )
  await page.reload()
  await expect(page.getByText("3/3", { exact: true })).toBeVisible()
  await expect(page.getByRole("textbox", { name: "请选择语气", exact: true })).toHaveValue("自然轻松")
  await page.getByRole("button", { name: "上一题", exact: true }).click()
  await expect(page.getByRole("button", { name: "保留", exact: true })).toHaveAttribute(
    "aria-pressed",
    "true",
  )
  await page.getByRole("button", { name: "上一题", exact: true }).click()
  await expect(page.getByRole("button", { name: "30秒", exact: true })).toHaveAttribute(
    "aria-pressed",
    "true",
  )
  await expect(page.getByRole("button", { name: "确认", exact: true })).toHaveCount(0)
  await expect(page.getByTestId("question-primary-action")).toHaveText("下一题")
  await expect(page.getByTestId("question-primary-action")).toBeEnabled()
})

test("custom input advances only on completion; earlier answers remain editable", async ({ page }) => {
  const state = await fixture(page)
  const input = page.getByRole("textbox", { name: "请选择时长", exact: true })
  await input.fill("45秒")
  await expect(page.getByText("1/3", { exact: true })).toBeVisible()
  await input.press("Enter")
  await expect(page.getByText("2/3", { exact: true })).toBeVisible()
  await page.getByRole("button", { name: "上一题", exact: true }).click()
  await expect(input).toHaveValue("45秒")
  await page.getByRole("button", { name: "60秒", exact: true }).click()
  await page.getByRole("button", { name: "保留", exact: true }).click()
  await page.getByRole("button", { name: "轻松", exact: true }).click()
  await expect(page.getByText("3/3", { exact: true })).toBeVisible()
  expect(state.answers).toEqual([])
  await page.getByRole("button", { name: "确认", exact: true }).click()
  await expect.poll(() => state.answers.length).toBe(1)
  expect(state.answers[0]).toEqual({ answers: [["60秒"], ["保留"], ["轻松"]] })
})

test("manual next can review unanswered pages, but cannot submit incomplete answers", async ({ page }) => {
  await fixture(page)
  await page.getByRole("navigation").getByRole("button", { name: "下一题", exact: true }).click()
  await page.getByRole("navigation").getByRole("button", { name: "下一题", exact: true }).click()
  await page.getByRole("button", { name: "轻松", exact: true }).click()
  await expect(page.getByRole("button", { name: "确认", exact: true })).toBeDisabled()
  await page.getByRole("button", { name: "上一题", exact: true }).click()
  await expect(page.getByText("2/3", { exact: true })).toBeVisible()
})

test("skip from an intermediate page resolves the whole ask without a partial answer", async ({ page }) => {
  const state = await fixture(page)
  await page.getByRole("button", { name: "30秒", exact: true }).click()
  await page.getByRole("button", { name: "跳过全部", exact: true }).click()
  await expect(page.getByTestId("question-primary-action")).toHaveCount(0)
  expect(state.rejections).toEqual(["fixture-ask"])
  expect(state.answers).toEqual([])
})
