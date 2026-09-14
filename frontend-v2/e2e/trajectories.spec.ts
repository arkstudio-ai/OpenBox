// Admin trajectory viewer in Chromium against the fixture server. FIXTURE
// EVIDENCE: the production routes, pages, hooks, sync engine and reducer run
// for real; only the transport is fake (e2e/helpers/trajectory-server.ts,
// which answers from real projector state at the requested watermark). Real
// authorization, deletion and /ws/agent isolation are verified separately
// against the running backend by the supervisor.
import { appendFileSync, mkdirSync, writeFileSync } from "node:fs"
import { expect, test, type Locator, type Page } from "@playwright/test"
import { EVENT_FAMILIES } from "../src/features/admin-trajectories/types/protocol"
import { replay } from "../src/features/admin-trajectories/utils/projector"
import {
  goldenSpec,
  listPopulation,
  loadGolden,
  richFixture,
  RICH_SESSION,
  unrecordedSpec,
  type GoldenFixture,
  type RichFixture,
} from "./helpers/trajectory-data"
import { installMetrics, readMetrics } from "./helpers/trajectory-metrics"
import { TRAJECTORY_API, TrajectoryFixtureServer } from "./helpers/trajectory-server"

const EVIDENCE = "test-results/trajectory-claude"
const SHOTS = `${EVIDENCE}/qa-screenshots`
const NETWORK_LOG = `${EVIDENCE}/qa-fixture-network.jsonl`
const FIXTURE = "/e2e/fixtures/trajectories.html"
const RICH = richFixture().seq
const LIST = "/app/admin/trajectories"

const EVENT_TYPES = new Set(
  Object.entries(EVENT_FAMILIES).flatMap(([family, actions]) =>
    actions.map((action) => `${family}.${action}`),
  ),
)
/** A namespace key rendered as text means a translation is missing. */
const RAW_KEY =
  /\b(?:access|agents|artifact|assistant|availability|block|captureLevel|common|export|field|inspector|kind|list|media|playback|position|preview|recordingStatus|relation|request|runStatus|schema|search|session|state|stats|status|summary|sync|tab|table|timeline|timing|timingSource|tool|toolbar|tools|usage)\.[a-z][A-Za-z]*(?:\.[a-z][A-Za-z]*)?\b/g

interface Harness {
  server: TrajectoryFixtureServer
  rich: RichFixture
  golden: GoldenFixture
  pageErrors: string[]
}

interface OpenOptions {
  lang?: "en-US" | "zh-CN"
  mode?: "dark"
  prepare?: (server: TrajectoryFixtureServer) => void
}

test.beforeAll(() => {
  mkdirSync(SHOTS, { recursive: true })
  writeFileSync(NETWORK_LOG, "")
})

function sessionRoute(sessionId: string, search = ""): string {
  return `${LIST}/sessions/${encodeURIComponent(sessionId)}${search ? `?${search}` : ""}`
}

function fixtureUrl(route: string, options: OpenOptions): string {
  const query = new URLSearchParams({ lang: options.lang ?? "en-US" })
  if (options.mode) query.set("mode", options.mode)
  return `${FIXTURE}?${query}#${route}`
}

async function openFixture(page: Page, route: string, options: OpenOptions = {}): Promise<Harness> {
  const golden = loadGolden("session_v1.json")
  const rich = richFixture()
  const server = new TrajectoryFixtureServer()
    .add(goldenSpec(golden, "Golden · 检查日志"))
    .add(rich.spec)
    .add(unrecordedSpec())
  for (const spec of listPopulation()) server.add(spec)
  options.prepare?.(server)
  const pageErrors: string[] = []
  page.on("pageerror", (error) => pageErrors.push(error.message))
  await server.install(page)
  await installMetrics(page)
  await page.goto(fixtureUrl(route, options))
  return { server, rich, golden, pageErrors }
}

const byTestId = (page: Page, id: string) => page.getByTestId(id)
const effectiveSeq = (page: Page) => page.getByTestId("trajectory-effective-seq")
const inspector = (page: Page) => page.getByTestId("trajectory-inspector")
const location = (page: Page) => page.getByTestId("fixture-location")
const row = (page: Page, recordId: string) =>
  page.locator(`[data-testid="trajectory-record-row"][data-record-id="${recordId}"]`)
const recordParam = (recordId: string) => `record=${encodeURIComponent(recordId)}`

async function waitReady(page: Page, seq?: number): Promise<void> {
  await expect(byTestId(page, "trajectory-workspace")).toBeVisible({ timeout: 45_000 })
  if (seq !== undefined) await expect(effectiveSeq(page)).toHaveAttribute("data-seq", String(seq))
}

async function seekTo(page: Page, seq: number): Promise<void> {
  const input = byTestId(page, "trajectory-seek-input")
  await input.fill(String(seq))
  await input.press("Enter")
  await waitReady(page, seq)
}

async function select(page: Page, recordId: string): Promise<void> {
  await row(page, recordId).click()
  await expect(inspector(page)).toHaveAttribute("data-record-id", recordId)
}

async function tabIds(page: Page): Promise<string[]> {
  return byTestId(page, "trajectory-inspector-tabs")
    .getByRole("tab")
    .evaluateAll((tabs) =>
      tabs.map((tab) => (tab.getAttribute("data-testid") ?? "").replace("trajectory-tab-", "")),
    )
}

async function openTab(page: Page, tab: string): Promise<Locator> {
  const button = byTestId(page, `trajectory-tab-${tab}`)
  await button.click()
  await expect(button).toHaveAttribute("aria-selected", "true")
  return inspector(page).getByRole("tabpanel")
}

async function objectUrlsOutstanding(page: Page): Promise<number> {
  const { objectUrls } = await readMetrics(page)
  return objectUrls.created - objectUrls.revoked
}

async function expectNoRawKeys(page: Page): Promise<void> {
  const text = await page.locator("main").innerText()
  const keys = [...text.matchAll(RAW_KEY)].map((match) => match[0]).filter((key) => !EVENT_TYPES.has(key))
  expect(keys, "translation keys rendered as text").toEqual([])
}

/** The viewer only reads trajectory data: no other API, socket or client frame, no read past the head. */
function expectReadOnlyTraffic(server: TrajectoryFixtureServer, allowExport = false): void {
  expect(server.unexpected, "requests outside the read-only trajectory API").toEqual([])
  expect(server.futureReads, "reads naming a watermark past the committed head").toEqual([])
  const allowed = new Set([`${TRAJECTORY_API}/ticket`])
  const writes = server.requests
    .filter(
      (request) =>
        request.method !== "GET" &&
        !allowed.has(request.path) &&
        !(allowExport && request.path.endsWith("/export")),
    )
    .map((request) => `${request.method} ${request.path}`)
  expect(writes, "non-read requests").toEqual([])
  const frames = server.socketLog.filter(
    (entry) => entry.kind === "client" && !["subscribe", "unsubscribe", "ping"].includes(entry.type ?? ""),
  )
  expect(frames, "client socket frames other than subscribe/unsubscribe/ping").toEqual([])
}

test.afterEach(({ page }, testInfo) => {
  const server = (page as Page & { __server?: TrajectoryFixtureServer }).__server
  if (!server) return
  const summary = {
    test: testInfo.title,
    status: testInfo.status,
    requests: server.requests.map(({ method, path, params, paged, status }) => ({
      method,
      path,
      params,
      paged,
      status,
    })),
    payloadReads: server.payloadReads.map(({ payloadId, throughSeq, status }) => ({
      payloadId,
      throughSeq,
      status,
    })),
    sockets: server.socketLog.map(({ kind, path, type, sessionId, code }) => ({
      kind,
      path,
      type,
      sessionId,
      code,
    })),
    unexpected: server.unexpected,
    futureReads: server.futureReads,
  }
  appendFileSync(NETWORK_LOG, `${JSON.stringify(summary)}\n`)
})

/** Keep the server reachable from afterEach for the sanitized network log. */
async function start(page: Page, route: string, options: OpenOptions = {}): Promise<Harness> {
  const harness = await openFixture(page, route, options)
  ;(page as Page & { __server?: TrajectoryFixtureServer }).__server = harness.server
  return harness
}

test("golden session: head and historical position match the backend projection", async ({ page }) => {
  const { server, golden, pageErrors } = await start(page, sessionRoute("session_a"))
  const historical = Number(golden.historical.through_seq)
  // The fixture serves exactly what the backend golden file expects, at both positions.
  expect(replay(golden.events)).toEqual(golden.expected_state)
  expect(server.stateAt("session_a", historical)).toEqual(golden.historical.state)

  const stats = golden.expected_statistics
  await waitReady(page, Number(stats.through_seq))
  await expect(byTestId(page, "trajectory-stat-requests").locator("dd")).toHaveText(
    String(stats.request_count),
  )
  await expect(byTestId(page, "trajectory-stat-tools").locator("dd")).toHaveText(String(stats.tool_count))
  await expect(byTestId(page, "trajectory-stat-errors").locator("dd")).toHaveText(String(stats.error_count))
  await expect(byTestId(page, "trajectory-stat-unknown").locator("dd")).toHaveText(
    String(stats.unknown_count),
  )
  await expect(byTestId(page, "trajectory-stat-input").locator("dd")).toHaveText(String(stats.input_tokens))
  await expect(byTestId(page, "trajectory-stat-output").locator("dd")).toHaveText(String(stats.output_tokens))
  await expect(byTestId(page, "trajectory-row-count")).toContainText(
    `of ${Object.keys(golden.expected_state.records).length} records`,
  )
  await expect(
    byTestId(page, "trajectory-agent-node").filter({ hasText: golden.expected_agents[0].name }),
  ).toBeVisible()
  await expect(row(page, "tool:call_a").getByText("Completed", { exact: true })).toBeVisible()

  await seekTo(page, historical)
  await expect(byTestId(page, "trajectory-row-count")).toContainText(
    `of ${Object.keys(golden.historical.state.records).length} records`,
  )
  await expect(row(page, "tool:call_a").getByText("Running", { exact: true })).toBeVisible()
  await expect(row(page, "question:q_a")).toHaveCount(0)
  await expect(
    byTestId(page, "trajectory-agent-node").filter({ hasText: golden.expected_agents[0].name }),
  ).toHaveCount(0)
  expectReadOnlyTraffic(server)
  expect(pageErrors).toEqual([])
})

test("list: filters, server sort and cursor paging live in the URL and survive a detail round trip", async ({
  page,
}) => {
  const { server, pageErrors } = await start(page, LIST)
  const rows = byTestId(page, "trajectory-list").locator("tbody tr")
  await expect(rows).toHaveCount(50, { timeout: 45_000 })
  const first = server.requests.find(
    (request) => request.path === `${TRAJECTORY_API}/sessions` && request.params.limit === "50",
  )
  expect(first?.params.sort).toBe("last_activity_desc")

  await page.getByRole("button", { name: "Next", exact: true }).click()
  await expect(page.getByText("Page 2", { exact: true })).toBeVisible()
  await expect(rows).toHaveCount(8)
  await expect(location(page)).toHaveText(/cursor=.+&trail=/)
  expect(
    server.requests.some(
      (request) => request.path.endsWith("/sessions") && request.paged && request.status === 200,
    ),
  ).toBe(true)
  const listUrl = (await location(page).textContent()) ?? ""
  const title = (await rows.first().getByRole("link").first().textContent()) ?? ""

  await rows.first().getByRole("link", { name: "View trajectory" }).click()
  await expect(byTestId(page, "trajectory-header")).toContainText(title)
  await expect(location(page)).toHaveText(/\/sessions\/[^?]+\?back=/)
  await byTestId(page, "trajectory-back").click()
  await expect(location(page)).toHaveText(listUrl)
  await expect(page.getByText("Page 2", { exact: true })).toBeVisible()
  await expect(rows.first()).toContainText(title)

  await page.getByLabel("User", { exact: true }).fill("bob")
  await page.getByRole("button", { name: "Apply", exact: true }).click()
  await expect(location(page)).toHaveText(/owner=bob/)
  await expect(location(page)).not.toHaveText(/cursor=/)
  await expect(rows.first()).toContainText("bob")
  for (const text of await rows.allInnerTexts()) expect(text).toContain("bob")
  expect(server.requests.some((request) => request.params.user_query === "bob")).toBe(true)

  await byTestId(page, "trajectory-list-sort").selectOption("last_activity_asc")
  await expect(location(page)).toHaveText(/sort=last_activity_asc/)
  await expect
    .poll(() =>
      server.requests.some(
        (request) => request.params.sort === "last_activity_asc" && request.params.user_query === "bob",
      ),
    )
    .toBe(true)

  // Unrecorded sessions are opt-in, and opening one reads its header only: no events, no subscription.
  await page.getByLabel("Include sessions that were never recorded").check()
  await page.getByRole("button", { name: "Apply", exact: true }).click()
  await byTestId(page, "trajectory-list").getByRole("link", { name: "Never recorded chat" }).click()
  await expect(byTestId(page, "trajectory-not-recorded")).toBeVisible()
  await page.waitForTimeout(1500)
  const neverPaths = server.requests
    .filter((request) => request.path.includes("sess-never-recorded"))
    .map((request) => request.path)
  expect(new Set(neverPaths)).toEqual(new Set([`${TRAJECTORY_API}/sessions/sess-never-recorded`]))
  expect(server.socketLog.filter((entry) => entry.sessionId === "sess-never-recorded")).toEqual([])
  expectReadOnlyTraffic(server)
  expect(pageErrors).toEqual([])
})

test("session: timeline, virtual record tree and inspector with type-specific tabs and captured fields", async ({
  page,
}) => {
  const { server, pageErrors } = await start(page, sessionRoute(RICH_SESSION))
  await waitReady(page, RICH.head)
  const header = byTestId(page, "trajectory-header")
  await expect(header).toContainText("mei")
  await expect(header).toContainText("Docs")
  await expect(header).toContainText(RICH_SESSION)

  const timeline = await byTestId(page, "trajectory-timeline").boundingBox()
  const list = await byTestId(page, "trajectory-record-list").boundingBox()
  const aside = await page.getByRole("complementary", { name: "Record details" }).boundingBox()
  expect(timeline && list && aside).toBeTruthy()
  expect(timeline!.y + timeline!.height).toBeLessThanOrEqual(list!.y)
  expect(aside!.x).toBeGreaterThanOrEqual(list!.x + list!.width - 2)
  await expect(byTestId(page, "trajectory-record-list")).toHaveAttribute(
    "aria-rowcount",
    String(Object.keys(server.stateAt(RICH_SESSION, RICH.head).records).length),
  )

  await select(page, "user:msg_rich_1")
  expect(await tabIds(page)).toEqual(["summary", "preview", "raw", "source", "events"])
  let panel = await openTab(page, "preview")
  await expect(panel).toContainText("请检查构建失败的截图")
  await expect(panel).toContainText("build-error.png")
  await expect(panel).toContainText("Accepted, not yet in the model's context.")
  panel = await openTab(page, "source")
  await expect(panel).toContainText("web")

  await select(page, "assistant:req_1")
  expect(await tabIds(page)).toEqual(["summary", "preview", "raw", "source", "events"])
  await openTab(page, "preview")
  const preview = byTestId(page, "trajectory-assistant-preview")
  await expect(preview).toContainText("我先运行测试确认失败原因，然后更新报告。")
  await expect(preview).toContainText("截图显示单元测试失败")
  await expect(preview).toContainText("2 tool calls")
  panel = await openTab(page, "raw")
  await expect(panel).toContainText("tool:call_bash")

  await select(page, "request:req_1")
  expect(await tabIds(page)).toEqual(["summary", "input", "options", "usage", "timing", "events"])
  await openTab(page, "input")
  const input = byTestId(page, "trajectory-request-input")
  await expect(input).toContainText("Prepared input (before provider format)")
  await expect(input).toContainText("You are the OpenBox agent.")
  await expect(input.getByTestId("trajectory-message")).toHaveCount(1)
  // The message's own previews. The collapsed raw snapshot renders the same wrappers again as
  // load-on-request references; they are present but must not load a second copy.
  const refs = input
    .getByTestId("trajectory-message")
    .locator('[data-testid="trajectory-message-media"] > [data-testid="trajectory-media-ref"]')
  await expect(refs).toHaveCount(3)
  await expect(input.getByTestId("trajectory-request-raw")).toBeAttached()
  await expect(input.getByTestId("trajectory-media")).toHaveCount(1)
  expect(await refs.evaluateAll((items) => items.map((item) => item.getAttribute("data-state")))).toEqual([
    "payload",
    "deleted",
    "not_recorded",
  ])
  await expect(refs.nth(0).getByTestId("trajectory-media")).toHaveJSProperty("naturalWidth", 240)
  expect(await refs.nth(0).getByTestId("trajectory-media").getAttribute("src")).toMatch(/^blob:/)
  await expect(refs.nth(1)).toContainText("Deleted — content is no longer available")
  await expect(refs.nth(1)).toContainText("Attachment deleted by its owner")
  await expect(refs.nth(2)).toContainText("Not recorded")
  await expect(input).toContainText("Run a shell command in the workspace sandbox.")
  await openTab(page, "options")
  const options = byTestId(page, "trajectory-request-options")
  await expect(options).toContainText("temperature")
  await expect(options).toContainText("0.2")
  await expect(options).toContainText(/4,?096/)
  panel = await openTab(page, "usage")
  await expect(byTestId(page, "trajectory-usage")).toContainText(/1,?200/)
  await openTab(page, "timing")
  await expect(byTestId(page, "trajectory-timing")).toContainText(/950/)

  await select(page, "tool:call_bash")
  expect(await tabIds(page)).toEqual(["summary", "arguments", "result", "schema", "timing", "events"])
  await expect(row(page, "tool:call_bash")).toContainText("Denied")
  await openTab(page, "arguments")
  const args = byTestId(page, "trajectory-tool-arguments")
  await expect(args).toContainText('{"command": "rm -rf build && npm test"}')
  await expect(args).toContainText("Never executed, so there are no execution arguments")
  await openTab(page, "result")
  await expect(byTestId(page, "trajectory-tool-result")).toContainText("Ended before execution")
  await openTab(page, "schema")
  const schema = byTestId(page, "trajectory-tool-schema")
  await expect(schema).toContainText("Sent to the model")
  await expect(schema.getByTestId("trajectory-schema-tree")).toContainText("command")
  await expect(schema).toContainText("Seconds before the command is stopped")
  await openTab(page, "timing")
  await expect(byTestId(page, "trajectory-timing")).toContainText("Never started executing")

  await select(page, "tool:call_write")
  await openTab(page, "result")
  const result = byTestId(page, "trajectory-tool-result")
  await expect(result.getByTestId("trajectory-payload")).toContainText("bytes_written")
  await expect(result).toContainText("Wrote report.md (3 lines).")
  await expect(result.getByTestId("trajectory-record-link").first()).toBeVisible()

  await select(page, "artifact:art_report")
  expect(await tabIds(page)).toEqual(["summary", "preview", "diff", "versions", "source", "events"])
  panel = await openTab(page, "diff")
  await expect(panel).toContainText("Tests fail in parser.spec.ts.")
  panel = await openTab(page, "versions")
  await expect(panel).toContainText("The file did not exist yet")

  await select(page, "permission:perm_push")
  expect(await tabIds(page)).toEqual(["summary", "request", "decision", "timing", "events"])
  panel = await openTab(page, "decision")
  await expect(panel).toContainText("Not produced yet at this position")

  await select(page, "question:q_commit")
  panel = await openTab(page, "questions")
  await expect(panel).toContainText("Read-only record. Answers cannot be submitted from here.")
  await expect(panel).toContainText("要把 report.md 提交到仓库吗？")
  await expect(panel.locator("input, textarea, select")).toHaveCount(0)
  panel = await openTab(page, "answer")
  await expect(panel).toContainText("Not produced yet at this position")

  await expectNoRawKeys(page)
  expectReadOnlyTraffic(server)
  expect(pageErrors).toEqual([])
})

test("replay at a fixed position shows only the recorded prefix: no later result, usage, agent, row or search hit", async ({
  page,
}) => {
  const at = RICH.writeRequested
  const future = ["artifact:art_report", "agent:agent_reviewer", "request:req_review", "question:q_commit"]
  const { server, pageErrors } = await start(
    page,
    sessionRoute(RICH_SESSION, `at=${at}&${recordParam("tool:call_write")}`),
  )
  await waitReady(page, at)
  await expect(byTestId(page, "trajectory-mode")).toHaveAttribute("data-live", "false")
  await expect(byTestId(page, "trajectory-header-position")).toHaveText(`Replay · at #${at}`)
  await expect(inspector(page)).toHaveAttribute("data-detail-seq", String(at))

  await openTab(page, "result")
  const result = byTestId(page, "trajectory-tool-result")
  await expect(result).toContainText("Not produced yet at this position")
  await expect(result.getByTestId("trajectory-payload")).toHaveCount(0)
  await expect(byTestId(page, "trajectory-workspace")).not.toContainText("Wrote report.md")
  for (const recordId of future) await expect(row(page, recordId)).toHaveCount(0)
  await expect(byTestId(page, "trajectory-agent-node").filter({ hasText: "Reviewer" })).toHaveCount(0)
  await expect(
    byTestId(page, "trajectory-timeline").locator('[data-record-ids="agent:agent_reviewer"]'),
  ).toHaveCount(0)

  // Nothing after the position was ever painted — including while the replay link was loading.
  const { seenRecordIds } = await readMetrics(page)
  expect(
    seenRecordIds.filter((recordId) => future.includes(recordId)),
    "future rows rendered at some point",
  ).toEqual([])
  const detailReads = server.requests.filter((request) => /\/(records\/|payloads\/)/.test(request.path))
  expect(detailReads.length).toBeGreaterThan(0)
  expect(
    detailReads.filter((request) => Number(request.params.through_seq) > at).map((request) => request.path),
  ).toEqual([])

  // Usage totals follow the position: the same before the second request, "not recorded" before the first usage event.
  const statInput = byTestId(page, "trajectory-stat-input").locator("dd")
  const inputAtPosition = await statInput.innerText()
  await seekTo(page, RICH.usage)
  await expect(statInput).toHaveText(inputAtPosition)
  await seekTo(page, RICH.usage - 1)
  await expect(statInput).toHaveText("Not recorded")

  // Search stays inside the shown position until the admin widens it; widening does not move the replay.
  await seekTo(page, at)
  await byTestId(page, "trajectory-search-toggle").click()
  await byTestId(page, "trajectory-search-input").fill("Reviewer")
  await byTestId(page, "trajectory-search-input").press("Enter")
  await expect(byTestId(page, "trajectory-search")).toContainText("No matches")
  expect(
    server.requests
      .filter((request) => request.path.endsWith("/search"))
      .map((request) => request.params.through_seq),
  ).toEqual([String(at)])
  await page.getByRole("radio", { name: new RegExp(`latest #${RICH.head}`) }).check()
  await expect(byTestId(page, "trajectory-search-hit").first()).toBeVisible()
  await expect(effectiveSeq(page)).toHaveAttribute("data-seq", String(at))

  await byTestId(page, "trajectory-return-live").click()
  await waitReady(page, RICH.head)
  await expect(statInput).not.toHaveText(inputAtPosition)
  await expect(row(page, "agent:agent_reviewer")).toHaveCount(1)
  expectReadOnlyTraffic(server)
  expect(pageErrors).toEqual([])
})

test("live head advances while the replay position, selection and URL stay put", async ({ page }) => {
  const at = RICH.writeRequested
  const { server, rich, pageErrors } = await start(
    page,
    sessionRoute(RICH_SESSION, `at=${at}&${recordParam("tool:call_write")}`),
  )
  await waitReady(page, at)
  await expect(byTestId(page, "trajectory-live-head")).toHaveAttribute("data-seq", String(RICH.head))
  await expect
    .poll(() =>
      server.socketLog.some((entry) => entry.type === "subscribe" && entry.sessionId === RICH_SESSION),
    )
    .toBe(true)

  server.append(RICH_SESSION, rich.liveTail)
  await expect(byTestId(page, "trajectory-live-head")).toHaveAttribute("data-seq", String(RICH.liveHead))
  await expect(byTestId(page, "trajectory-newer-count")).toContainText(`${RICH.liveHead - at} newer events`)
  await expect(effectiveSeq(page)).toHaveAttribute("data-seq", String(at))
  await expect(inspector(page)).toHaveAttribute("data-record-id", "tool:call_write")
  await expect(inspector(page)).toHaveAttribute("data-detail-seq", String(at))
  await expect(location(page)).toHaveText(new RegExp(`[?&]at=${at}(&|$)`))
  await expect(row(page, "question:q_commit")).toHaveCount(0)

  await byTestId(page, "trajectory-return-live").click()
  await waitReady(page, RICH.liveHead)
  await expect(row(page, "question:q_commit")).toContainText("Completed")
  await expect(inspector(page)).toHaveAttribute("data-record-id", "tool:call_write")
  expectReadOnlyTraffic(server)
  expect(pageErrors).toEqual([])
})

test("event and Step stepping, typed seek, play/pause and reload restore the position", async ({ page }) => {
  const at = RICH.writeRequested
  const { server, rich, pageErrors } = await start(page, sessionRoute(RICH_SESSION, `at=${at}`))
  const stepStarts = rich.spec.events
    .filter((event) => event.type === "step.started")
    .map((event) => Number(event.seq))
  await waitReady(page, at)

  await byTestId(page, "trajectory-next-event").click()
  await waitReady(page, at + 1)
  await expect(row(page, "tool:call_write")).toContainText("Running")
  await byTestId(page, "trajectory-next-step").click()
  await waitReady(page, stepStarts[1])
  await expect(row(page, "step:step_2")).toHaveCount(1)
  await byTestId(page, "trajectory-previous-step").click()
  await waitReady(page, stepStarts[0])
  await expect(row(page, "tool:call_bash")).toHaveCount(0)
  await byTestId(page, "trajectory-previous-event").click()
  await waitReady(page, stepStarts[0] - 1)

  await seekTo(page, RICH.bashRequested)
  await expect(row(page, "tool:call_bash")).toContainText("Pending")
  await seekTo(page, RICH.bashRequested - 1)
  await expect(row(page, "tool:call_bash")).toHaveCount(0)

  await byTestId(page, "trajectory-play").click()
  await expect
    .poll(async () => Number(await effectiveSeq(page).getAttribute("data-seq")), { timeout: 15_000 })
    .toBeGreaterThan(RICH.bashRequested)
  await byTestId(page, "trajectory-play").click()
  await expect(byTestId(page, "trajectory-play")).toHaveAttribute("aria-pressed", "false")
  const paused = (await effectiveSeq(page).getAttribute("data-seq")) ?? ""
  await page.waitForTimeout(1200)
  await expect(effectiveSeq(page)).toHaveAttribute("data-seq", paused)

  await select(page, "user:msg_rich_1")
  await expect(location(page)).toHaveText(new RegExp(`at=${paused}.*record=user`))
  await page.reload()
  await waitReady(page, Number(paused))
  await expect(inspector(page)).toHaveAttribute("data-record-id", "user:msg_rich_1")
  expectReadOnlyTraffic(server)
  expect(pageErrors).toEqual([])
})

test("protected $media: shown from the payload endpoint, released on deletion, never read before it existed", async ({
  page,
}) => {
  const { server, pageErrors } = await start(page, sessionRoute(RICH_SESSION, recordParam("request:req_1")))
  await waitReady(page, RICH.head)
  await openTab(page, "input")
  const first = byTestId(page, "trajectory-media-ref").nth(0)
  await expect(first.getByTestId("trajectory-media")).toHaveJSProperty("naturalWidth", 240)
  expect(
    server.payloadReads.some(
      (read) => read.payloadId === "pl_img_ok" && read.throughSeq === RICH.head && read.status === 200,
    ),
  ).toBe(true)

  // Deleted while on screen: the next revalidation (remount) replaces the image and revokes its object URL.
  server.setPayloadState(RICH_SESSION, "pl_img_ok", "deleted")
  await openTab(page, "options")
  await openTab(page, "input")
  await expect(first).toContainText("Deleted — content is no longer available")
  await expect(byTestId(page, "trajectory-media")).toHaveCount(0)
  await expect.poll(() => objectUrlsOutstanding(page)).toBe(0)
  expect(server.payloadReads.filter((read) => read.payloadId === "pl_img_ok").at(-1)?.status).toBe(410)

  // Deletion wins over history: replaying to the request's own position still shows it deleted.
  await seekTo(page, RICH.prepared)
  await select(page, "request:req_1")
  await openTab(page, "input")
  await expect(byTestId(page, "trajectory-media-ref").nth(0)).toContainText(
    "Deleted — content is no longer available",
  )

  await seekTo(page, RICH.writeFinished - 1)
  await select(page, "tool:call_write")
  await openTab(page, "result")
  await expect(byTestId(page, "trajectory-tool-result")).toContainText("Not produced yet at this position")
  await seekTo(page, RICH.writeFinished)
  await expect(byTestId(page, "trajectory-tool-result").getByTestId("trajectory-payload")).toContainText(
    "bytes_written",
  )
  expect(
    server.payloadReads.filter((read) => read.status === 404),
    "payload requested before it existed",
  ).toEqual([])
  expectReadOnlyTraffic(server)
  expect(pageErrors).toEqual([])
})

test("refused read (403): loaded content, media and socket are dropped and reading stops", async ({
  page,
}) => {
  const { server } = await start(page, sessionRoute(RICH_SESSION, recordParam("request:req_1")))
  await waitReady(page, RICH.head)
  await openTab(page, "input")
  await expect(byTestId(page, "trajectory-media")).toHaveJSProperty("naturalWidth", 240)
  await expect.poll(() => server.openSockets()).toBe(1)

  server.deny(403)
  await expect(byTestId(page, "trajectory-access-denied")).toContainText(
    "Your account no longer has platform admin access.",
    { timeout: 15_000 },
  )
  await expect(byTestId(page, "trajectory-record-row")).toHaveCount(0)
  await expect(inspector(page)).toHaveCount(0)
  await expect(byTestId(page, "trajectory-media")).toHaveCount(0)
  await expect.poll(() => objectUrlsOutstanding(page)).toBe(0)
  await expect.poll(() => server.openSockets()).toBe(0)
  const settled = server.requests.length
  await page.waitForTimeout(3000)
  expect(
    server.requests.slice(settled).map((request) => `${request.method} ${request.path} ${request.status}`),
    "reads after the refusal latched",
  ).toEqual([])
  await page.screenshot({ path: `${SHOTS}/qa-access-denied-en.png` })
  expectReadOnlyTraffic(server)
})

test("watermark socket closed with 4403 latches the refusal without waiting for a REST failure", async ({
  page,
}) => {
  const { server } = await start(page, sessionRoute(RICH_SESSION))
  await waitReady(page, RICH.head)
  await expect.poll(() => server.openSockets()).toBe(1)
  server.closeSockets(4403)
  await expect(byTestId(page, "trajectory-access-denied")).toBeVisible({ timeout: 15_000 })
  await expect(byTestId(page, "trajectory-record-row")).toHaveCount(0)
  const settled = server.requests.length
  await page.waitForTimeout(2500)
  expect(
    server.requests.slice(settled).map((request) => `${request.method} ${request.path}`),
    "reads after the socket refusal",
  ).toEqual([])
  expectReadOnlyTraffic(server)
})

test("role loss in the signed-in account clears what was shown and stops reading", async ({ page }) => {
  const { server } = await start(page, sessionRoute(RICH_SESSION, recordParam("request:req_1")))
  await waitReady(page, RICH.head)
  await expect.poll(() => server.openSockets()).toBe(1)
  await page.evaluate(() =>
    (
      window as unknown as { __trajectoryFixture: { setRole: (role: string) => void } }
    ).__trajectoryFixture.setRole("user"),
  )
  await expect(byTestId(page, "trajectory-access-denied")).toContainText("Your role changed.")
  await expect(byTestId(page, "trajectory-record-row")).toHaveCount(0)
  await expect(inspector(page)).toHaveCount(0)
  await expect.poll(() => server.openSockets()).toBe(0)
  const settled = server.requests.length
  await page.waitForTimeout(2500)
  expect(
    server.requests.slice(settled).map((request) => `${request.method} ${request.path}`),
    "reads after the role change",
  ).toEqual([])
  expectReadOnlyTraffic(server)
})

test("deleting the watched session clears what was shown", async ({ page }) => {
  const { server } = await start(page, sessionRoute(RICH_SESSION, recordParam("request:req_1")))
  await waitReady(page, RICH.head)
  await expect(inspector(page)).toBeVisible()
  server.deleteSession(RICH_SESSION)
  await expect(
    page.locator('[data-testid="trajectory-session-wiped"], [data-testid="trajectory-session-error"]'),
  ).toBeVisible({ timeout: 15_000 })
  await expect(byTestId(page, "trajectory-record-row")).toHaveCount(0)
  await expect(inspector(page)).toHaveCount(0)
  await expect(byTestId(page, "trajectory-media")).toHaveCount(0)
  expectReadOnlyTraffic(server)
})

test("list refused with 403 shows the access notice and no rows", async ({ page }) => {
  const { server } = await start(page, LIST, { prepare: (fixture) => fixture.deny(403) })
  await expect(byTestId(page, "trajectory-access-denied")).toBeVisible({ timeout: 45_000 })
  await expect(page.locator("tbody tr")).toHaveCount(0)
  expectReadOnlyTraffic(server)
})

test("read-only viewer: no control can approve, answer, send, resume, cancel or execute", async ({
  page,
}) => {
  const { server, pageErrors } = await start(
    page,
    sessionRoute(RICH_SESSION, recordParam("question:q_commit")),
  )
  await waitReady(page, RICH.head)
  await openTab(page, "questions")
  const controls = await byTestId(page, "trajectory-session")
    .locator("button, a[href], input, select, textarea, [role='button']")
    .evaluateAll((elements) => {
      // Controls named after recorded content ("AI reply · …", a question's text) only select that record:
      // timeline marks, record links, agent nodes, record rows, search hits and inspector breadcrumbs.
      const navigation =
        "[data-mark-index], [data-testid='trajectory-record-link'], [data-testid='trajectory-agent-node'], [data-testid='trajectory-record-row'] *, [data-testid='trajectory-search-hit'] *, [data-testid='trajectory-inspector-header'] nav *"
      const named = (element: Element) =>
        (
          element.getAttribute("aria-label") ??
          element.getAttribute("title") ??
          element.textContent ??
          ""
        ).trim()
      const candidates = elements.filter((element) => element.getAttribute("role") !== "tab")
      return {
        actions: candidates.filter((element) => !element.matches(navigation)).map(named),
        navigation: candidates.filter((element) => element.matches(navigation)).map(named),
      }
    })
  expect(controls.actions.length).toBeGreaterThan(10)
  expect(controls.navigation.length, "record navigation controls").toBeGreaterThan(0)
  const acting =
    /\b(approve|allow|deny|reject|answer|reply|respond|send|resume|continue|cancel|stop|execute|take over)\b|批准|允许|拒绝|回答|发送|继续执行|取消运行|停止|执行命令/i
  expect(
    controls.actions.filter((name) => acting.test(name)),
    "controls that would act on the watched session",
  ).toEqual([])
  await byTestId(page, "trajectory-timeline").locator("[data-mark-index]").first().click()
  await expect(inspector(page)).toBeVisible()
  await expect(inspector(page).getByRole("tabpanel").locator("input, textarea, select")).toHaveCount(0)
  expectReadOnlyTraffic(server)
  expect(pageErrors).toEqual([])
})

test("visual: dark English desktop session with a tool result", async ({ page }) => {
  const { server, pageErrors } = await start(
    page,
    sessionRoute(RICH_SESSION, recordParam("tool:call_write")),
    { lang: "en-US", mode: "dark" },
  )
  await waitReady(page, RICH.head)
  await openTab(page, "result")
  await expect(byTestId(page, "trajectory-payload")).toBeVisible()
  await expect(page.locator("html")).toHaveAttribute("data-mode", "dark")
  await expectNoRawKeys(page)
  await page.screenshot({ path: `${SHOTS}/qa-session-dark-en.png` })
  expectReadOnlyTraffic(server)
  expect(pageErrors).toEqual([])
})

test("visual: Chinese narrow session and Chinese list and replay", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  const { server, pageErrors } = await start(page, sessionRoute(RICH_SESSION, recordParam("request:req_1")), {
    lang: "zh-CN",
  })
  await waitReady(page, RICH.head)
  await openTab(page, "input")
  await expect(byTestId(page, "trajectory-media").first()).toHaveJSProperty("naturalWidth", 240)
  await expectNoRawKeys(page)
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)
  await page.screenshot({ path: `${SHOTS}/qa-session-zh-narrow.png`, fullPage: true })
  expect.soft(overflow, "horizontal page overflow at 390px (px)").toBeLessThanOrEqual(1)

  await page.setViewportSize({ width: 1440, height: 900 })
  // In-app back link: a goto that only changes the hash would not remount the fixture's MemoryRouter.
  await byTestId(page, "trajectory-back").click()
  await expect(location(page)).toHaveText(LIST)
  await expect(byTestId(page, "trajectory-list").locator("tbody tr").first()).toBeVisible({ timeout: 45_000 })
  await expectNoRawKeys(page)
  await page.screenshot({ path: `${SHOTS}/qa-list-zh.png` })

  // Same document, new hash: reload so the fixture mounts the replay route from its URL.
  await page.goto(
    fixtureUrl(sessionRoute(RICH_SESSION, `at=${RICH.writeRequested}&${recordParam("tool:call_write")}`), {
      lang: "zh-CN",
    }),
  )
  await page.reload()
  await waitReady(page, RICH.writeRequested)
  await openTab(page, "result")
  await expect(byTestId(page, "trajectory-tool-result")).toContainText("此位置尚未产生")
  await expectNoRawKeys(page)
  await page.screenshot({ path: `${SHOTS}/qa-replay-zh.png` })
  expectReadOnlyTraffic(server)
  expect(pageErrors).toEqual([])
})
