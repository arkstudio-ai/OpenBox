// In-page timing and resource counters for the trajectory evidence. Times are
// read inside the page with performance.now(): at the submit event that starts
// a seek, and at the first animation frame after the DOM shows the finished
// state. Playwright's polling interval therefore neither inflates nor hides a
// number. Nothing here reads request headers, tokens or storage.
import type { Page } from "@playwright/test"

export interface PageMetrics {
  marks: Record<string, number>
  longTasks: Array<{ start: number; duration: number }>
  objectUrls: { created: number; revoked: number }
  /** Every record id that was ever rendered as a table row, in order of first appearance. */
  seenRecordIds: string[]
}

export interface FrameTime {
  /** performance.now() in the page. */
  now: number
  /** Wall clock (performance.timeOrigin + now), comparable with Date.now() in the test process. */
  epoch: number
}

/** Must run before navigation. Records first paint of preview/rows/workspace, submits, long tasks, object URLs and rendered rows. */
export async function installMetrics(page: Page): Promise<void> {
  await page.addInitScript(() => {
    const state: PageMetrics = {
      marks: {},
      longTasks: [],
      objectUrls: { created: 0, revoked: 0 },
      seenRecordIds: [],
    }
    ;(window as unknown as { __trajectoryMetrics: PageMetrics }).__trajectoryMetrics = state
    document.addEventListener("submit", () => (state.marks.submit = performance.now()), true)
    try {
      new PerformanceObserver((list) => {
        for (const entry of list.getEntries())
          state.longTasks.push({ start: entry.startTime, duration: entry.duration })
      }).observe({ type: "longtask", buffered: true })
    } catch {
      // Long task timing unavailable in this browser.
    }
    const create = URL.createObjectURL.bind(URL)
    const revoke = URL.revokeObjectURL.bind(URL)
    URL.createObjectURL = (object: Blob | MediaSource) => {
      state.objectUrls.created += 1
      return create(object)
    }
    URL.revokeObjectURL = (url: string) => {
      state.objectUrls.revoked += 1
      revoke(url)
    }
    const firsts: Array<[string, string]> = [
      ["summaryPreview", '[data-testid="trajectory-summary-preview"]'],
      ["firstRow", '[data-testid="trajectory-record-row"]'],
      ["workspace", '[data-testid="trajectory-workspace"]'],
    ]
    const pending = new Set(firsts.map(([name]) => name))
    const seen = new Set<string>()
    const ROW = '[data-testid="trajectory-record-row"][data-record-id]'
    const note = (element: Element) => {
      const id = element.getAttribute("data-record-id")
      if (id && !seen.has(id)) {
        seen.add(id)
        state.seenRecordIds.push(id)
      }
    }
    new MutationObserver((mutations) => {
      for (const mutation of mutations) {
        if (
          mutation.type === "attributes" &&
          mutation.target instanceof Element &&
          mutation.target.matches(ROW)
        )
          note(mutation.target)
        for (const node of mutation.addedNodes) {
          if (!(node instanceof Element)) continue
          if (node.matches(ROW)) note(node)
          node.querySelectorAll(ROW).forEach(note)
        }
      }
      for (const [name, selector] of firsts) {
        if (!pending.has(name) || !document.querySelector(selector)) continue
        pending.delete(name)
        requestAnimationFrame(() => (state.marks[name] = performance.now()))
      }
    }).observe(document, {
      childList: true,
      subtree: true,
      attributes: true,
      attributeFilter: ["data-record-id"],
    })
  })
}

export function readMetrics(page: Page): Promise<PageMetrics> {
  return page.evaluate(() => (window as unknown as { __trajectoryMetrics: PageMetrics }).__trajectoryMetrics)
}

/**
 * Start waiting in the page for `selector`; resolves at the first animation
 * frame after it matches. Create the waiter before the action that should
 * produce the state, then await it.
 */
export function frameWhen(page: Page, selector: string, timeoutMs = 180_000): Promise<FrameTime> {
  return page.evaluate(
    ({ selector, timeoutMs }) =>
      new Promise<FrameTime>((resolve, reject) => {
        let settled = false
        const finish = () => {
          if (settled) return
          settled = true
          observer.disconnect()
          window.clearTimeout(timer)
          requestAnimationFrame(() => {
            const now = performance.now()
            resolve({ now, epoch: performance.timeOrigin + now })
          })
        }
        const observer = new MutationObserver(() => {
          if (document.querySelector(selector)) finish()
        })
        const timer = window.setTimeout(() => {
          observer.disconnect()
          reject(new Error(`timed out waiting for ${selector}`))
        }, timeoutMs)
        observer.observe(document, { childList: true, subtree: true, attributes: true, characterData: true })
        if (document.querySelector(selector)) finish()
      }),
    { selector, timeoutMs },
  )
}

/** Nearest-rank percentile, ceil(p × n): the convention of the storage verification report. */
export function percentile(samples: readonly number[], p: number): number {
  const sorted = [...samples].sort((a, b) => a - b)
  return sorted[Math.max(0, Math.ceil(p * sorted.length) - 1)]
}

export function round(value: number): number {
  return Math.round(value * 10) / 10
}
