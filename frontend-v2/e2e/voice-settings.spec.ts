import { expect, test } from "@playwright/test"
import { assistantApi, sockets } from "./helpers/voice"

const AUDIO = "qwen-audio-3.1-realtime-plus"
const OMNI = "qwen3.8-omni-flash-realtime"
const models = [
  { id: AUDIO, name: "Audio 3.1", tier: "expert" },
  { id: OMNI, name: "Omni", tier: "standard" },
]
const catalogs = {
  [AUDIO]: [
    { id: "longanqian_v3.1", name: "龙安浅" },
    { id: "longanhuan_v3.1", name: "龙安欢" },
  ],
  [OMNI]: [
    { id: "Tina", name: "甜甜" },
    { id: "Serena", name: "苏瑶" },
  ],
}

for (const width of [1440, 390]) {
  test(`voice model and per-model voices persist at ${width}px`, async ({ page }, info) => {
    await page.setViewportSize({ width, height: 920 })
    await assistantApi(page)
    await sockets(page)
    await page.route("**/api/assistant/profile", (route) =>
      route.fulfill({
        json: {
          profile: {},
          decided: {},
          intro: { status: "done", steps: {}, nudged: false },
        },
      }),
    )
    let model = AUDIO
    const choices = { [AUDIO]: "longanqian_v3.1", [OMNI]: "Tina" }
    await page.route("**/api/assistant/voice/*", (route) => {
      if (route.request().method() === "PUT") {
        const body = route.request().postDataJSON()
        model = body.model ?? model
        if (body.voice) choices[model] = body.voice
      }
      return route.fulfill({
        json: {
          model,
          models,
          default_model: AUDIO,
          selected: choices[model],
          default: catalogs[model]![0]!.id,
          voices: catalogs[model]!.map((v) => ({
            ...v,
            gender: "female",
            lang: "zh",
            description: "中文女声",
            description_en: "Chinese female voice",
          })),
        },
      })
    })
    const errors: string[] = []
    page.on("pageerror", (error) => errors.push(error.message))
    await page.goto("/app/settings/voice")
    const expert = page.getByRole("button", { name: /专家.*Audio 3.1/ })
    const standard = page.getByRole("button", { name: /普通.*Omni/ })
    await expect(expert).toHaveAttribute("aria-pressed", "true")
    await expect(page.getByText("默认模型", { exact: true })).toBeVisible()
    await page.getByRole("button", { name: /^龙安欢 longanhuan/ }).click()
    await expect(page.getByRole("button", { name: /^龙安欢 longanhuan/ })).toHaveAttribute(
      "aria-pressed",
      "true",
    )
    await page.screenshot({ path: info.outputPath("expert.png") })
    await standard.click()
    await expect(standard).toHaveAttribute("aria-pressed", "true")
    await expect(page.getByText("龙安浅", { exact: true })).toHaveCount(0)
    await page.getByRole("button", { name: /^苏瑶 Serena/ }).click()
    await expect(page.getByRole("button", { name: /^苏瑶 Serena/ })).toHaveAttribute("aria-pressed", "true")
    await page.reload()
    await expect(standard).toHaveAttribute("aria-pressed", "true")
    await expect(page.getByRole("button", { name: /^苏瑶 Serena/ })).toHaveAttribute("aria-pressed", "true")
    await page.screenshot({ path: info.outputPath("standard.png") })
    await expert.click()
    await expect(page.getByRole("button", { name: /^龙安欢 longanhuan/ })).toHaveAttribute(
      "aria-pressed",
      "true",
    )
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    expect(errors).toEqual([])
  })
}
