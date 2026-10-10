import { expect, test } from "@playwright/test"
import { assistantApi, sockets } from "./helpers/voice"

for (const viewport of [{ width: 1440, height: 920 }, { width: 390, height: 844 }]) {
  test(`dismiss reminders and find all seven requests in My tasks at ${viewport.width}px`, async ({ page }, info) => {
    await page.setViewportSize(viewport)
    const api = await assistantApi(page)
    await sockets(page)
    await page.route("**/api/assistant/profile", (route) => route.fulfill({ json: {
      profile: {}, decided: {}, intro: { status: "done", steps: {}, nudged: false },
    } }))
    await page.route("**/api/memories/recalled/**", (route) => route.fulfill({ json: { recalls: {} } }))
    const errors: string[] = []
    page.on("pageerror", (error) => errors.push(error.message))
    const mutations: string[] = []
    page.on("request", (request) => {
      if (request.method() !== "GET" && new URL(request.url()).pathname.startsWith("/api/assistant/")) mutations.push(request.url())
    })
    await page.route("**/api/assistant/requests/waiting", (route) => route.fulfill({ json: {
      items: Array.from({ length: 7 }, (_, i) => ({
        id: `waiting-${i}`, session_id: `video-${i}`, session_title: `视频制作 ${i + 1}`, project_name: "默认空间",
        questions: [{ header: "制作确认", question: `请确认第 ${i + 1} 个视频的时长与风格。` }],
      })),
    } }))
    await page.goto("/app/assistant")
    const reminder = page.getByRole("button", { name: "有 7 件待处理事项，前往「我的任务」查看" })
    await expect(reminder).toBeVisible()
    await expect(page.getByText("待处理 7", { exact: true })).toBeVisible()
    await expect(page.getByText("请确认第 1 个视频的时长与风格。")).toHaveCount(0)
    await page.evaluate(() => (document.documentElement.dataset.mode = "dark"))
    await page.screenshot({ path: info.outputPath("compact-reminder.png") })
    await page.getByRole("button", { name: "收起提醒（待处理事项保留在我的任务中）" }).click()
    await expect(reminder).toHaveCount(0)
    await page.reload()
    const tasks = page.getByRole("button", { name: /我的任务.*7/ })
    await expect(tasks).toBeVisible()
    await expect(reminder).toHaveCount(0)
    await page.screenshot({ path: info.outputPath("dismissed-reminder.png") })
    await tasks.click()
    const sheet = page.getByRole("dialog", { name: "我的任务", exact: true })
    await expect(sheet.getByRole("link", { name: "去回答" })).toHaveCount(7)
    await expect(sheet.getByText("请确认第 1 个视频的时长与风格。")).toBeVisible()
    await expect(sheet.getByText("还没有交给我的事")).toHaveCount(0)
    await page.screenshot({ path: info.outputPath("pending-tasks-drawer.png") })
    const width = await page.evaluate(() => ({ scroll: document.documentElement.scrollWidth, viewport: innerWidth }))
    expect(width.scroll).toBeLessThanOrEqual(width.viewport)
    await sheet.getByRole("link", { name: "去回答" }).first().click()
    await expect(page).toHaveURL(/\/app\/s\/video-0$/)
    await expect(sheet).toHaveCount(0)
    expect(mutations).toEqual([])
    expect(errors).toEqual([])
    // Opening the target chat may read its fixture-less details; the reminder flow itself must not.
    expect(api.unknown.filter((path) => !path.includes("video-0"))).toEqual([])
  })
}
