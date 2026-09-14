// Scale evidence: one session of 100,000 events / 10,004 records (10,000 tool
// calls, baseline, turn, run, step) opened, sought, scrolled and followed live through the production
// page in Chromium. FIXTURE EVIDENCE: the page, sync engine and reducer are
// real; the transport is Playwright interception on loopback with no network
// latency, and the frontend is the Vite dev server (unbundled modules, React
// development build, StrictMode) — not a production build or deployment.
// Every time is taken inside the page (helpers/trajectory-metrics). Results,
// raw samples, environment and targets go to qa-scale-metrics.json; a missed
// target is reported there, not hidden by a retry.
import { mkdirSync, writeFileSync } from "node:fs"
import { createRequire } from "node:module"
import { cpus, platform, release, totalmem } from "node:os"
import { expect, test, type Page } from "@playwright/test"
import { SCALE_SESSION, scaleFixture, scaleToolEvents } from "./helpers/trajectory-data"
import {
  frameWhen,
  installMetrics,
  percentile,
  readMetrics,
  round,
  type PageMetrics,
} from "./helpers/trajectory-metrics"
import { TrajectoryFixtureServer } from "./helpers/trajectory-server"

const EVIDENCE = "test-results/trajectory-claude"
const OUT = `${EVIDENCE}/qa-scale-metrics.json`
const URL_PATH = `/e2e/fixtures/trajectories.html?lang=en-US#/app/admin/trajectories/sessions/${SCALE_SESSION}`
/** SESSION_TRAJECTORY_IMPLEMENTATION_PLAN §16.2 initial targets ("to be calibrated", specified environment). */
const TARGETS = { firstSummaryP95Ms: 500, seekP95Ms: 1000, visibleOutputP95Ms: 100 }
const SAMPLES = { open: 20, coldSeek: 3, repeatedSeek: 30, scroll: 30, select: 10, liveAppend: 20 }
const ROW_BOUND = 100

interface Distribution {
  n: number
  min: number
  median: number
  /** Nearest-rank p95; only reported with at least 20 samples. */
  p95: number | null
  max: number
  samples: number[]
}

function distribution(samples: readonly number[]): Distribution {
  const sorted = [...samples].sort((a, b) => a - b)
  return {
    n: samples.length,
    min: round(sorted[0]),
    median: round(percentile(samples, 0.5)),
    p95: samples.length >= 20 ? round(percentile(samples, 0.95)) : null,
    max: round(sorted[sorted.length - 1]),
    samples: samples.map(round),
  }
}

function verdict(value: number | null, target: number): string {
  if (value === null) return "not assessed (fewer than 20 samples)"
  return value <= target ? `met (${value} ms <= ${target} ms)` : `MISSED (${value} ms > ${target} ms)`
}

interface OpenSample {
  firstRowMs: number
  summaryPreviewMs: number | null
  workspaceMs: number
  recordsResponseEndMs: number | null
  checkpointResponseEndMs: number | null
  longestTaskMs: number
  renderedRows: number
  rowCount: string | null
  heapBytes: number | null
}

async function openSample(page: Page): Promise<OpenSample> {
  await page.reload()
  await expect(page.getByTestId("trajectory-workspace")).toBeVisible({ timeout: 180_000 })
  await page.waitForTimeout(300)
  return page.evaluate(() => {
    const metrics = (window as unknown as { __trajectoryMetrics: PageMetrics }).__trajectoryMetrics
    const resources = performance.getEntriesByType("resource") as PerformanceResourceTiming[]
    const records = resources
      .filter((entry) => entry.name.includes("/records?"))
      .sort((a, b) => a.startTime - b.startTime)[0]
    const checkpoint = resources.find((entry) => entry.name.includes("/checkpoint"))
    const memory = (performance as Performance & { memory?: { usedJSHeapSize: number } }).memory
    return {
      firstRowMs: metrics.marks.firstRow,
      summaryPreviewMs: metrics.marks.summaryPreview ?? null,
      workspaceMs: metrics.marks.workspace,
      recordsResponseEndMs: records ? records.responseEnd : null,
      checkpointResponseEndMs: checkpoint ? checkpoint.responseEnd : null,
      longestTaskMs: metrics.longTasks.reduce((longest, task) => Math.max(longest, task.duration), 0),
      renderedRows: document.querySelectorAll('[data-testid="trajectory-record-row"]').length,
      rowCount:
        document.querySelector('[data-testid="trajectory-record-list"]')?.getAttribute("aria-rowcount") ??
        null,
      heapBytes: memory ? memory.usedJSHeapSize : null,
    }
  })
}

async function seekSample(
  page: Page,
  server: TrajectoryFixtureServer,
  target: number,
): Promise<{ ms: number; longestTaskMs: number }> {
  const before = (await readMetrics(page)).longTasks.length
  const ready = frameWhen(page, `[data-testid="trajectory-effective-seq"][data-seq="${target}"]`)
  const input = page.getByTestId("trajectory-seek-input")
  await input.fill(String(target))
  await input.press("Enter")
  const done = await ready
  const metrics = await readMetrics(page)
  // The table shows the records that exist at the target, not the head's.
  const expected = Object.keys(server.stateAt(SCALE_SESSION, target).records).length
  await expect(page.getByTestId("trajectory-row-count")).toContainText(`of ${expected} records`)
  return {
    ms: done.now - metrics.marks.submit,
    longestTaskMs: metrics.longTasks
      .slice(before)
      .reduce((longest, task) => Math.max(longest, task.duration), 0),
  }
}

test("100,000 events / 10,004 records: first summary, seek, scroll, selection, live output and virtualization", async ({
  page,
  browser,
}) => {
  test.setTimeout(30 * 60_000)
  const generationStart = performance.now()
  const scale = scaleFixture()
  const generationMs = performance.now() - generationStart
  expect(scale.eventCount).toBe(100_000)

  const server = new TrajectoryFixtureServer().add(scale.spec)
  const projectionStart = performance.now()
  const headRecords = Object.keys(server.stateAt(SCALE_SESSION, scale.eventCount).records).length
  const serverProjectionMs = performance.now() - projectionStart
  expect(headRecords).toBe(scale.recordCount)
  await server.install(page)
  await installMetrics(page)

  // Warm-up: dev-server module compilation and the fake server's checkpoint serialisation. Not sampled.
  await page.goto(URL_PATH)
  await expect(page.getByTestId("trajectory-workspace")).toBeVisible({ timeout: 600_000 })

  const opens: OpenSample[] = []
  for (let index = 0; index < SAMPLES.open; index += 1) opens.push(await openSample(page))
  for (const sample of opens) {
    expect(sample.rowCount).toBe(String(scale.recordCount))
    expect(sample.renderedRows, "rendered rows must stay bounded by virtualization").toBeLessThan(ROW_BOUND)
  }

  // Cold seeks: each target is before the replay segment loaded so far, so it reads a checkpoint
  // at or before the target plus events through the target only.
  const coldSeeks: number[] = []
  const coldLongest: number[] = []
  for (const target of [75_000, 50_000, 25_000]) {
    const sample = await seekSample(page, server, target)
    coldSeeks.push(sample.ms)
    coldLongest.push(sample.longestTaskMs)
  }

  // Repeated seeks to 30 scattered historical targets in [25,001, 99,990]. Mixed, not warm-cache: a
  // target may be inside the loaded replay segment (no read), a short step past it (events only) or
  // elsewhere (checkpoint plus tail through the target).
  const repeatedSeeks: number[] = []
  const repeatedLongest: number[] = []
  for (let index = 0; index < SAMPLES.repeatedSeek; index += 1) {
    const sample = await seekSample(page, server, 25_001 + (((index + 1) * 24_977) % 74_990))
    repeatedSeeks.push(sample.ms)
    repeatedLongest.push(sample.longestTaskMs)
  }

  // Scrolling the virtual table: time until a row covers the new viewport top, plus one frame.
  const scroll = await page.evaluate(async (count) => {
    const group = document.querySelector('[data-testid="trajectory-record-list"] [role="rowgroup"]')
    const scroller = group?.parentElement
    if (!scroller) throw new Error("record scroller not found")
    const frame = () =>
      new Promise<number>((resolve) => requestAnimationFrame(() => resolve(performance.now())))
    const samples: number[] = []
    let maxRows = 0
    for (let index = 0; index < count; index += 1) {
      await frame()
      const target = Math.floor(
        ((((index + 1) * 7919) % 997) / 997) * (scroller.scrollHeight - scroller.clientHeight),
      )
      const started = performance.now()
      scroller.scrollTop = target
      for (;;) {
        await frame()
        const top = scroller.getBoundingClientRect().top
        const covered = [
          ...scroller.querySelectorAll<HTMLElement>('[data-testid="trajectory-record-row"]'),
        ].some((element) => {
          const offset = element.getBoundingClientRect().top - top + scroller.scrollTop
          return offset <= scroller.scrollTop + 1 && offset + element.offsetHeight > scroller.scrollTop
        })
        if (covered || performance.now() - started > 5000) break
      }
      samples.push((await frame()) - started)
      maxRows = Math.max(maxRows, scroller.querySelectorAll('[data-testid="trajectory-record-row"]').length)
    }
    return { samples, maxRows }
  }, SAMPLES.scroll)
  expect(scroll.maxRows).toBeLessThan(ROW_BOUND)

  // Selecting a record in replay: detail read at the position and its summary painted.
  const selection = await page.evaluate(async (count) => {
    const frame = () =>
      new Promise<number>((resolve) => requestAnimationFrame(() => resolve(performance.now())))
    const rows = [
      ...document.querySelectorAll<HTMLElement>('[data-testid="trajectory-record-row"][data-kind="tool"]'),
    ].slice(0, count)
    const samples: number[] = []
    for (const element of rows) {
      const id = element.getAttribute("data-record-id")
      const selector = `[data-testid="trajectory-inspector"][data-record-id="${id}"] [data-testid="trajectory-summary"]`
      const started = performance.now()
      element.click()
      while (!document.querySelector(selector) && performance.now() - started < 10_000) await frame()
      samples.push((await frame()) - started)
    }
    return samples
  }, SAMPLES.select)
  expect(selection).toHaveLength(SAMPLES.select)

  // Live output: commit one more tool call (10 events), time until the live position shows it.
  await page.getByTestId("trajectory-return-live").click()
  await expect(page.getByTestId("trajectory-effective-seq")).toHaveAttribute(
    "data-seq",
    String(scale.eventCount),
    { timeout: 60_000 },
  )
  const liveAppends: number[] = []
  for (let index = 0; index < SAMPLES.liveAppend; index += 1) {
    const head = server.head(SCALE_SESSION)
    const events = scaleToolEvents(
      head,
      scale.toolRecords + index,
      scale.spec.events[scale.spec.events.length - 1].occurred_at,
    )
    const ready = frameWhen(
      page,
      `[data-testid="trajectory-effective-seq"][data-seq="${head + events.length}"]`,
    )
    await page.waitForTimeout(50)
    const committed = Date.now()
    server.append(SCALE_SESSION, events)
    liveAppends.push((await ready).epoch - committed)
  }
  await expect(page.getByTestId("trajectory-row-count")).toContainText(
    `of ${scale.recordCount + SAMPLES.liveAppend} records`,
  )
  const finalRows = await page.getByTestId("trajectory-record-row").count()
  expect(finalRows).toBeLessThan(ROW_BOUND)

  const firstSummary = distribution(opens.map((sample) => sample.firstRowMs))
  const firstSummaryAfterResponse = distribution(
    opens
      .filter((sample) => sample.recordsResponseEndMs !== null)
      .map((sample) => sample.firstRowMs - (sample.recordsResponseEndMs ?? 0)),
  )
  const repeated = distribution(repeatedSeeks)
  const live = distribution(liveAppends)
  const require = createRequire(import.meta.url)
  const report = {
    kind: "Fixture browser evidence: real page/sync/reducer, fake loopback transport, Vite dev build. Not a native API, backend or production-build measurement.",
    generatedAt: new Date().toISOString(),
    environment: {
      os: `${platform()} ${release()}`,
      cpu: cpus()[0]?.model ?? "unknown",
      logicalCores: cpus().length,
      memoryGiB: round(totalmem() / 2 ** 30),
      node: process.version,
      playwright: (require("@playwright/test/package.json") as { version: string }).version,
      browser: `chromium ${browser.version()}`,
      viewport: page.viewportSize(),
      frontend: "Vite dev server 127.0.0.1:3101 (unbundled ES modules, React development build, StrictMode)",
      transport:
        "Playwright route/WebSocket interception answering from real projector state; no network latency",
      serverCheckpointInterval: 1000,
      eventPageSize: 500,
      recordPageSize: 100,
    },
    fixture: {
      events: scale.eventCount,
      records: scale.recordCount,
      toolRecords: scale.toolRecords,
      generationMs: round(generationMs),
      fakeServerHeadProjectionMs: round(serverProjectionMs),
    },
    results: {
      firstSummaryRowFromNavigationMs: firstSummary,
      firstSummaryRowAfterRecordsResponseMs: firstSummaryAfterResponse,
      workspaceReadyFromNavigationMs: distribution(opens.map((sample) => sample.workspaceMs)),
      summaryPreviewShownInSamples: opens.filter((sample) => sample.summaryPreviewMs !== null).length,
      checkpointResponseEndMs: distribution(
        opens
          .filter((sample) => sample.checkpointResponseEndMs !== null)
          .map((sample) => sample.checkpointResponseEndMs ?? 0),
      ),
      openLongestTaskMs: distribution(opens.map((sample) => sample.longestTaskMs)),
      heapAfterOpenMiB: distribution(
        opens
          .filter((sample) => sample.heapBytes !== null)
          .map((sample) => (sample.heapBytes ?? 0) / 2 ** 20),
      ),
      coldSeekMs: {
        ...distribution(coldSeeks),
        targets: [75_000, 50_000, 25_000],
        note: "each reads a checkpoint at or before the target plus events through the target only (not up to the live base); 3 samples, no percentile",
      },
      coldSeekLongestTaskMs: coldLongest.map(round),
      repeatedSeekMs: {
        ...repeated,
        note: "30 scattered historical targets in [25,001, 99,990] after the cold seeks, mixed per target: inside the loaded replay segment (no read), up to 2,000 events past it (events only) or elsewhere (checkpoint plus tail through the target). Not a warm-cache measurement",
      },
      repeatedSeekLongestTaskMs: distribution(repeatedLongest),
      scrollRenderMs: distribution(scroll.samples),
      selectRecordDetailMs: {
        ...distribution(selection),
        note: "replay mode (no live settle delay); includes the detail read at the position",
      },
      liveOutputCommitToPaintMs: {
        ...live,
        note: "Date.now() before the fake server commits and announces 10 events, to the first frame showing the new live position",
      },
      virtualization: {
        ariaRowCount: scale.recordCount,
        renderedRowsAfterOpen: distribution(opens.map((sample) => sample.renderedRows)),
        maxRenderedRowsWhileScrolling: scroll.maxRows,
        renderedRowsAfterLiveAppends: finalRows,
        bound: ROW_BOUND,
      },
    },
    targets: {
      source:
        "docs/SESSION_TRAJECTORY_IMPLEMENTATION_PLAN.md §16.2 (initial targets, to be calibrated in a specified environment)",
      firstSummaryP95: verdict(firstSummary.p95, TARGETS.firstSummaryP95Ms),
      ordinarySeekP95: verdict(repeated.p95, TARGETS.seekP95Ms),
      visibleOutputP95: verdict(live.p95, TARGETS.visibleOutputP95Ms),
      coldSeek: `3 samples only (max ${round(Math.max(...coldSeeks))} ms); not a percentile`,
    },
  }
  mkdirSync(EVIDENCE, { recursive: true })
  writeFileSync(OUT, `${JSON.stringify(report, null, 2)}\n`)
  expect(server.unexpected, "requests outside the trajectory API").toEqual([])
  expect(server.futureReads, "reads past the committed head").toEqual([])
})
