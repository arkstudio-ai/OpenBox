import { expect, test, type Page } from "@playwright/test"

const items = [
  { label: "精简开头", prompt: "请保留原来的语气，把刚才的开头精简到三句话。", mode: "send" },
  { label: "换个表达角度", prompt: "请换一个角度表达刚才的方案，并保留原有约束。", mode: "send" },
  { label: "准备发布", prompt: "请帮我准备发布，我要发布的平台是：", mode: "draft" },
]
const group = (page: Page) => page.getByRole("group", { name: /下一步建议|Suggested next steps/ })

async function fixture(page: Page, options: { failSend?: boolean; noChips?: boolean; longHistory?: boolean } = {}) {
  const sent: { text: string; model: string; client_message_id: string }[] = []
  const offsets: number[] = []
  const errors: string[] = []
  page.on("pageerror", (error) => errors.push(error.message))
  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname
    if (!path.startsWith("/api/")) return route.continue()
    if (path.endsWith("/prompt_async")) {
      sent.push(route.request().postDataJSON())
      return route.fulfill({ status: options.failSend ? 503 : 200, json: { ok: !options.failSend } })
    }
    if (path.endsWith("/message")) {
      const messages = [
      { id: "u1", session_id: "suggestions-fixture", role: "user", created_at: "2026-09-10T00:00:00Z",
        parts: [{ type: "text", id: "u-text", text: "请帮我优化这份产品介绍文案。" }] },
      ...(options.longHistory ? Array.from({ length: 201 }, (_, index) => ({
        id: `step-${index}`, session_id: "suggestions-fixture", role: "assistant", finish: "tool_calls",
        created_at: "2026-09-10T00:00:01Z", parts: [],
      })) : []),
      { id: "a1", session_id: "suggestions-fixture", role: "assistant", finish: "stop", created_at: "2026-09-10T00:01:00Z",
        parts: [
          { type: "text", id: "a-text", channel: "final", text: Array.from({ length: 14 }, (_, i) =>
            `### ${i + 1}. 让每一个想法，都有下一步\n\n从一句想法开始，把资料、分析和创作串联起来。你负责判断方向，OpenBox 帮你推进具体工作。`).join("\n\n") },
          ...(options.noChips ? [] : [{ type: "suggestions", id: "p1", items }]),
        ] },
      ]
      const offset = Number(new URL(route.request().url()).searchParams.get("offset") ?? 0)
      offsets.push(offset)
      return route.fulfill({ json: messages.slice(offset, offset + 200) })
    }
    if (path === "/api/agent/config") return route.fulfill({ json: {
      models: [{ id: "openai/chat-picked", name: "Chat model", variants: ["low", "high"] },
        { id: "openai/alternative", name: "Alternative model" }], default_model: "openai/default",
      video_models: [{ id: "video-test", name: "Video model", resolutions: ["720p"] }],
      default_video_model: "video-test", default_video_resolution: "720p",
    } })
    if (path === "/api/containers") return route.fulfill({ json: { containers: [] } })
    if (path === "/api/auth/me/preferences") return route.fulfill({ json: { extra: {} } })
    return route.fulfill({ json: [] })
  })
  await page.goto("/e2e/fixtures/suggestions.html")
  await expect(page.getByRole("textbox")).toBeVisible()
  return { sent, errors, offsets }
}

test("chips sit above the composer; full prompt sends with the currently selected model", async ({ page }) => {
  const { sent, errors } = await fixture(page)
  await expect(group(page).getByRole("button")).toHaveCount(3)
  const row = await group(page).boundingBox()
  const input = await page.getByRole("textbox").boundingBox()
  expect(row!.y + row!.height).toBeLessThan(input!.y)
  await page.getByRole("button", { name: "Chat model" }).click()
  await page.getByRole("button", { name: "Alternative model" }).click()
  await group(page).getByRole("button", { name: "发送建议：精简开头" }).click()
  await expect.poll(() => sent.length).toBe(1)
  expect(sent[0]).toMatchObject({ text: items[0].prompt, model: "openai/alternative" })
  expect(sent[0].client_message_id).toBeTruthy()
  await expect(group(page)).toHaveCount(0)
  await expect(page.getByText(items[0].prompt, { exact: true })).toBeVisible()
  expect(errors).toEqual([])
})

test("drafts hide chips; incomplete suggestions fill and focus without sending", async ({ page }) => {
  const { sent } = await fixture(page)
  const textarea = page.getByRole("textbox")
  await textarea.fill("我的未发送草稿")
  await expect(group(page)).toHaveCount(0)
  await textarea.fill("")
  await expect(group(page)).toBeVisible()
  await group(page).getByRole("button", { name: "编辑建议：准备发布" }).click()
  await expect(textarea).toHaveValue(items[2].prompt)
  await expect(textarea).toBeFocused()
  expect(sent).toHaveLength(0)
})

test("failed sends restore the full suggestion as a draft", async ({ page }) => {
  const { sent } = await fixture(page, { failSend: true })
  await group(page).getByRole("button", { name: "发送建议：精简开头" }).click()
  await expect(page.getByRole("textbox")).toHaveValue(items[0].prompt)
  expect(sent).toHaveLength(1)
  await expect(group(page)).toHaveCount(0)
})

test("running, waiting, error, permissions and read-only hide chips", async ({ page }) => {
  await fixture(page)
  for (const state of ["busy", "waiting_input", "error"]) {
    await page.getByRole("button", { name: state, exact: true }).click()
    await expect(group(page)).toHaveCount(0)
    await page.getByRole("button", { name: "idle", exact: true }).click()
    await expect(group(page)).toBeVisible()
  }
  for (const name of ["Toggle permission", "Toggle read only"]) {
    await page.getByRole("button", { name }).click()
    await expect(group(page)).toHaveCount(0)
    await page.getByRole("button", { name }).click()
    await expect(group(page)).toBeVisible()
  }
})

test("scrolling up hides chips and returning to the bottom restores them", async ({ page }) => {
  await fixture(page)
  await expect(group(page)).toBeVisible()
  await page.getByText("14. 让每一个想法，都有下一步", { exact: true }).hover()
  await page.mouse.wheel(0, -1600)
  await expect(group(page)).toHaveCount(0)
  await page.mouse.wheel(0, 4000)
  await expect(group(page)).toBeVisible()
})

test("cached suggestions survive reload; late events cannot leak into a new turn", async ({ page }) => {
  await fixture(page)
  await page.reload()
  await expect(group(page).getByRole("button")).toHaveCount(3)
  await group(page).getByRole("button", { name: "发送建议：精简开头" }).click()
  await page.getByRole("button", { name: "Late event" }).click()
  await page.getByRole("button", { name: "idle", exact: true }).click()
  await expect(group(page)).toHaveCount(0)
})

test("late suggestion event fills an initially empty row", async ({ page }) => {
  await fixture(page, { noChips: true })
  await expect(group(page)).toHaveCount(0)
  await page.getByRole("button", { name: "Late event" }).click()
  await expect(group(page).getByRole("button", { name: "发送建议：补充更多案例" })).toBeVisible()
})

test("refresh reaches the latest answer beyond the first 200 messages", async ({ page }) => {
  const { offsets, errors } = await fixture(page, { longHistory: true })
  await expect(group(page).getByRole("button")).toHaveCount(3)
  expect(offsets).toEqual([0, 200])
  await page.reload()
  await expect(group(page).getByRole("button")).toHaveCount(3)
  expect(offsets).toEqual([0, 200, 0, 200])
  expect(errors).toEqual([])
})

test("light/dark and Chinese/English layouts stay in bounds on desktop and mobile", async ({ page }, testInfo) => {
  const { errors } = await fixture(page)
  for (const size of [{ width: 1280, height: 900 }, { width: 390, height: 844 }]) {
    await page.setViewportSize(size)
    for (const mode of ["Light", "Dark"]) {
      await page.getByRole("button", { name: mode, exact: true }).click()
      for (const language of ["中文", "English"]) {
        await page.getByRole("button", { name: language, exact: true }).click()
        await expect(group(page)).toBeVisible()
        const bounds = await group(page).boundingBox()
        expect(bounds!.x).toBeGreaterThanOrEqual(0)
        expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(size.width)
        expect(await page.locator("body").evaluate((body) => body.scrollWidth)).toBeLessThanOrEqual(size.width)
        await page.screenshot({ path: testInfo.outputPath(`${size.width}-${mode}-${language}.png`) })
      }
    }
  }
  expect(errors).toEqual([])
})
