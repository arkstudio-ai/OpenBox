// A voice call from the assistant's top bar, through the real workspace shell:
// dial, talk, hand a request to the assistant, collapse, hang up. The server
// side is scripted here (helpers/voice.ts); see playwright.voice-call.config.ts.
import { expect, test } from "@playwright/test"
import { assistantApi, sockets, voiceDebug } from "./helpers/voice"

const READY = {
  type: "ready",
  call_id: "call-e2e",
  model: "qwen3.8-omni-flash-realtime",
  input_sample_rate: 16000,
  output_sample_rate: 24000,
  max_seconds: 1800,
  price_date: "2026-10-07",
}
const COST = {
  type: "cost",
  total_yuan: "0.003500",
  confirmed_yuan: "0.003500",
  provisional_yuan: "0.000000",
  costs_yuan: {
    input_text: "0.000100",
    input_audio: "0.001200",
    output_text: "0.000050",
    output_audio: "0.002150",
  },
  settled_rounds: 2,
  unreported_rounds: 0,
  pending: false,
  final: false,
  price_date: "2026-10-07",
}

test("a call from the top bar: dial, talk, hand off, collapse, hang up", async ({ page }, info) => {
  const api = await assistantApi(page)
  const voice = await sockets(page)
  const errors: string[] = []
  page.on("pageerror", (error) => errors.push(error.message))
  await page.goto("/app/assistant")
  await expect(page.getByText("第 10 个问题：帮我看看项目进展。")).toBeVisible()

  await page.getByRole("button", { name: "和个人助理通话" }).click()
  const dock = page.getByRole("region", { name: "个人助理" })
  const status = dock.getByRole("status")
  await expect(status).toHaveText("正在接通…")
  // The entry now brings the window back rather than dialling a second call.
  await expect(page.getByRole("button", { name: "通话中" })).toBeVisible()

  const server = await voice.nextCall()
  expect(server.url).toContain("/ws/assistant/voice?ticket=voice-ticket")
  expect(api.tickets).toContainEqual({ audience: "voice" })
  // The fake microphone is running; nothing may be sent before `ready`.
  await page.waitForTimeout(500)
  expect(server.audio).toEqual([])

  server.send(READY)
  await expect(status).toHaveText("已接通")
  await expect(dock.getByText("直接说话就好，随时可以打断。")).toBeVisible()
  await expect(dock.getByText(/^00:0\d$/)).toBeVisible()
  // 100 ms PCM16 packets at 16 kHz.
  await expect.poll(() => server.audio.length).toBeGreaterThan(3)
  expect(new Set(server.audio)).toEqual(new Set([3200]))

  server.send({ type: "phase", value: "listening" })
  await expect(status).toHaveText("我在听")
  server.send({ type: "phase", value: "thinking" })
  await expect(status).toHaveText("想一下…")
  server.send({ type: "phrase", key: "greeting" })
  server.send({ type: "phase", value: "speaking" })
  server.sendAudio(800)
  await expect(status).toHaveText("在说话")

  // A request handed to the text assistant: the conversation shows that turn.
  const handedOff = page.locator('[data-turn-key="u-1"]')
  await expect(handedOff).not.toBeInViewport()
  server.send({ type: "turn", turn_id: "turn-1", state: "accepted", inbox_id: "inbox-1", message_id: "u-1" })
  server.send({ type: "phase", value: "working", working: true })
  await expect(status).toHaveText("在办，稍等…")
  await expect(handedOff).toBeInViewport()
  // Let the reply finish first: while it still plays, the phase reads as speaking.
  await expect.poll(async () => (await voiceDebug(page))?.state.playing).toBe(false)
  server.send({ type: "phase", value: "listening", working: true, late: true })
  await expect(status).toHaveText("我在听还在办…")
  server.send(COST)
  await expect(dock.getByText("¥0.0035")).toBeVisible()
  await page.screenshot({ path: info.outputPath("voice-expanded.png") })
  await page.evaluate(() => (document.documentElement.dataset.mode = "dark"))
  await dock.screenshot({ path: info.outputPath("voice-expanded-dark.png") })
  await page.evaluate(() => (document.documentElement.dataset.mode = "light"))

  // Collapse to the pill; the call goes on across pages.
  await dock.getByRole("button", { name: "收起" }).click()
  const pill = dock.getByRole("button", { name: "展开" })
  await expect(pill).toBeVisible()
  await dock.screenshot({ path: info.outputPath("voice-pill.png") })
  await expect(dock.getByRole("button", { name: "静音" })).toHaveCount(0)
  // A client-side route change, as the sidebar makes: a full reload would end the call.
  await page.evaluate(() => {
    window.history.pushState({}, "", "/app/settings")
    window.dispatchEvent(new PopStateEvent("popstate"))
  })
  await expect(page).toHaveURL(/\/app\/settings/)
  await expect(pill).toBeVisible()
  await pill.click()
  await expect(dock.getByRole("button", { name: "收起" })).toBeVisible()

  // Mute is local: the frames keep coming, only silent.
  await dock.getByRole("button", { name: "静音" }).click()
  await expect(dock.getByRole("button", { name: "取消静音" })).toBeVisible()
  const sentBeforeMute = server.audio.length
  await expect.poll(() => server.audio.length).toBeGreaterThan(sentBeforeMute + 2)
  await dock.getByRole("button", { name: "取消静音" }).click()

  // One click hangs up; the panel shows what the server settled.
  await dock.getByRole("button", { name: "挂断" }).click()
  await expect(status).toHaveText("正在挂断…")
  await expect.poll(() => server.messages).toContainEqual({ type: "stop" })
  const framesAtHangUp = server.audio.length
  server.send({
    type: "ended",
    reason: "hangup",
    duration_seconds: 14,
    pending_turns: 1,
    cost: { ...COST, final: true },
  })
  await server.close()
  await expect(dock.getByText("通话已结束")).toBeVisible()
  await expect(dock.getByText("时长 00:14 · 费用约 ¥0.0035")).toBeVisible()
  await expect(dock.getByText("还有 1 件事在办，结果会写在对话里。")).toBeVisible()
  await expect(dock.getByRole("button", { name: "重新拨打" })).toBeVisible()
  await dock.screenshot({ path: info.outputPath("voice-ended.png") })
  expect(server.audio.length).toBe(framesAtHangUp)

  const marks = (await voiceDebug(page))?.timings ?? []
  const names = marks.map((mark) => mark.mark)
  const order = [
    "click",
    "mic_granted",
    "ticket",
    "socket_open",
    "ready",
    "first_audio_received",
    "first_audio_started",
    "ended",
  ]
  expect(names).toEqual(
    expect.arrayContaining([
      ...order,
      "phase:listening",
      "phase:thinking",
      "phase:speaking",
      "phrase:greeting",
      "turn:accepted",
      "phase:working",
    ]),
  )
  const at = (name: string) => marks.find((mark) => mark.mark === name)!.t
  for (let i = 1; i < order.length; i++) expect(at(order[i])).toBeGreaterThanOrEqual(at(order[i - 1]))

  await dock.getByRole("button", { name: "关闭" }).click()
  await expect(dock).toHaveCount(0)
  expect(errors).toEqual([])
  expect(api.unknown).toEqual([])
})

test("another device already on a call: the panel says so and offers no redial", async ({ page }) => {
  await assistantApi(page)
  const voice = await sockets(page)
  await page.goto("/app/assistant")
  await page.getByRole("button", { name: "和个人助理通话" }).click()
  const server = await voice.nextCall()
  await server.close(4009)
  const dock = page.getByRole("region", { name: "个人助理" })
  await expect(dock.getByText("你在另一台设备上正在通话。")).toBeVisible()
  await expect(dock.getByRole("button", { name: "重新拨打" })).toHaveCount(0)
  await expect(dock.getByRole("button", { name: "关闭" })).toBeVisible()
})

test("an interruption cuts the model's voice and goes back to listening", async ({ page }) => {
  await assistantApi(page)
  const voice = await sockets(page)
  await page.goto("/app/assistant")
  await page.getByRole("button", { name: "和个人助理通话" }).click()
  const server = await voice.nextCall()
  server.send(READY)
  const status = page.getByRole("region", { name: "个人助理" }).getByRole("status")
  server.send({ type: "phase", value: "speaking" })
  for (let i = 0; i < 6; i++) server.sendAudio(500)
  // The server already moved on; three seconds of speech are still queued.
  server.send({ type: "phase", value: "listening" })
  await expect(status).toHaveText("在说话")
  server.send({ type: "playback.clear" })
  await expect(status).toHaveText("我在听")
  const names = ((await voiceDebug(page))?.timings ?? []).map((mark) => mark.mark)
  expect(names).toEqual(expect.arrayContaining(["playback_clear_received", "playback_stopped"]))
})

test("no entry when voice calls are switched off", async ({ page }) => {
  await assistantApi(page, { voiceEnabled: false })
  await sockets(page)
  await page.goto("/app/assistant")
  await expect(page.getByText("第 10 个问题：帮我看看项目进展。")).toBeVisible()
  await expect(page.getByRole("button", { name: "我的任务" })).toBeVisible()
  await expect(page.getByRole("button", { name: "和个人助理通话" })).toHaveCount(0)
})
