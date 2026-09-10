import { chromium, type Browser, type Page, type ElementHandle } from "playwright";
import type {
  GetPageRequest,
  GetPageResponse,
  ListPagesResponse,
  ServerInfoResponse,
  ViewportSize,
} from "./types";
import { getSnapshotScript } from "./snapshot/browser-script";

/**
 * Options for waiting for page load
 */
export interface WaitForPageLoadOptions {
  /** Maximum time to wait in ms (default: 10000) */
  timeout?: number;
  /** How often to check page state in ms (default: 50) */
  pollInterval?: number;
  /** Minimum time to wait even if page appears ready in ms (default: 100) */
  minimumWait?: number;
  /** Wait for network to be idle (no pending requests) (default: true) */
  waitForNetworkIdle?: boolean;
}

/**
 * Result of waiting for page load
 */
export interface WaitForPageLoadResult {
  /** Whether the page is considered loaded */
  success: boolean;
  /** Document ready state when finished */
  readyState: string;
  /** Number of pending network requests when finished */
  pendingRequests: number;
  /** Time spent waiting in ms */
  waitTimeMs: number;
  /** Whether timeout was reached */
  timedOut: boolean;
  /**
   * A captcha / risk-control challenge spotted on the loaded page, if any.
   * Heuristic and vendor-based — treat it as a strong hint, confirm with a
   * snapshot, then hand the page to the user (`desktop_takeover`).
   */
  challenge: ChallengeInfo | null;
}

/** What blocked the page, as far as the DOM can tell. */
export interface ChallengeInfo {
  /** Which anti-bot system drew it, or "text" when only the wording matched. */
  vendor: "geetest" | "aliyun" | "tencent" | "cloudflare" | "hcaptcha" | "recaptcha" | "text";
  /** The selector or phrase that matched, for the model's report. */
  hint: string;
}

/**
 * Look for a human-verification challenge on the page: known vendor widgets
 * first, then the phrases sites put on their risk-control pages. Only visible
 * matches count, so a dormant reCAPTCHA badge in the footer does not trigger.
 * Returns null when nothing matched. This never blocks and never tries to
 * solve anything — it exists so the agent notices the wall on the first look
 * instead of after a few failed clicks.
 */
export async function detectChallenge(page: Page): Promise<ChallengeInfo | null> {
  try {
    return await page.evaluate(() => {
      /* eslint-disable @typescript-eslint/no-explicit-any */
      const g = globalThis as { document?: any; getComputedStyle?: any };
      /* eslint-enable @typescript-eslint/no-explicit-any */
      const doc = g.document;
      if (!doc) return null;

      // Plain JS only: this function is serialised and runs inside the page.
      const visible = (el: any) => {
        if (!el) return false;
        const style = g.getComputedStyle ? g.getComputedStyle(el) : null;
        if (style && (style.display === "none" || style.visibility === "hidden")) return false;
        const rect = el.getBoundingClientRect ? el.getBoundingClientRect() : null;
        return !rect || (rect.width > 0 && rect.height > 0);
      };
      const firstVisible = (selector: string) => {
        const nodes = doc.querySelectorAll(selector);
        for (let i = 0; i < nodes.length; i += 1) if (visible(nodes[i])) return nodes[i];
        return null;
      };

      const vendors: Array<[string, string]> = [
        ["geetest", ".geetest_panel, .geetest_holder, .geetest_captcha, [class*='geetest_']"],
        ["aliyun", "#nc_1_wrapper, .nc-container, .nc_wrapper, #baxia-dialog-content, .baxia-dialog"],
        ["tencent", "#tcaptcha_iframe, #tcaptcha_transform, iframe[src*='captcha.qq.com'], iframe[src*='turing.captcha']"],
        ["cloudflare", "#challenge-form, #challenge-stage, .cf-challenge, iframe[src*='challenges.cloudflare.com'], .cf-turnstile"],
        ["hcaptcha", "iframe[src*='hcaptcha.com']"],
        ["recaptcha", "iframe[src*='recaptcha/api2/bframe'], iframe[src*='recaptcha/enterprise/bframe']"],
      ];
      for (let i = 0; i < vendors.length; i += 1) {
        const vendor = vendors[i][0];
        const selector = vendors[i][1];
        const hit = firstVisible(selector);
        if (hit) {
          const id = hit.id ? "#" + hit.id : "";
          const cls = typeof hit.className === "string" && hit.className ? "." + hit.className.split(/\s+/)[0] : "";
          return { vendor: vendor, hint: (hit.tagName || "").toLowerCase() + id + cls };
        }
      }

      const text = (doc.body && doc.body.innerText ? doc.body.innerText : "").slice(0, 20000);
      const phrases = [
        /安全验证/, /滑动验证/, /拖动滑块/, /向右滑动/, /请完成验证/, /请完成安全验证/, /人机验证/,
        /按顺序点击/, /点击图中/, /访问过于频繁/, /环境异常/, /操作频繁/, /请输入验证码/,
        /verify (?:that )?you are (?:a )?human/i, /checking your browser/i, /unusual traffic/i,
        /complete the security check/i, /slide to verify/i, /i'm not a robot/i,
      ];
      for (let i = 0; i < phrases.length; i += 1) {
        const m = text.match(phrases[i]);
        if (m) return { vendor: "text", hint: m[0] };
      }
      return null;
    });
  } catch {
    // Navigating or detached — nothing to report.
    return null;
  }
}

interface PageLoadState {
  documentReadyState: string;
  documentLoading: boolean;
  pendingRequests: PendingRequest[];
}

interface PendingRequest {
  url: string;
  loadingDurationMs: number;
  resourceType: string;
}

/**
 * Wait for a page to finish loading using document.readyState and performance API.
 *
 * Uses browser-use's approach of:
 * - Checking document.readyState for 'complete'
 * - Monitoring pending network requests via Performance API
 * - Filtering out ads, tracking, and non-critical resources
 * - Graceful timeout handling (continues even if timeout reached)
 */
export async function waitForPageLoad(
  page: Page,
  options: WaitForPageLoadOptions = {}
): Promise<WaitForPageLoadResult> {
  const {
    timeout = 10000,
    pollInterval = 50,
    minimumWait = 100,
    waitForNetworkIdle = true,
  } = options;

  const startTime = Date.now();
  let lastState: PageLoadState | null = null;

  // Wait minimum time first
  if (minimumWait > 0) {
    await new Promise((resolve) => setTimeout(resolve, minimumWait));
  }

  // Poll until ready or timeout
  while (Date.now() - startTime < timeout) {
    try {
      lastState = await getPageLoadState(page);

      // Check if document is complete
      const documentReady = lastState.documentReadyState === "complete";

      // Check if network is idle (no pending critical requests)
      const networkIdle = !waitForNetworkIdle || lastState.pendingRequests.length === 0;

      if (documentReady && networkIdle) {
        return {
          success: true,
          readyState: lastState.documentReadyState,
          pendingRequests: lastState.pendingRequests.length,
          waitTimeMs: Date.now() - startTime,
          timedOut: false,
          challenge: await detectChallenge(page),
        };
      }
    } catch {
      // Page may be navigating, continue polling
    }

    await new Promise((resolve) => setTimeout(resolve, pollInterval));
  }

  // Timeout reached - return current state
  return {
    success: false,
    readyState: lastState?.documentReadyState ?? "unknown",
    pendingRequests: lastState?.pendingRequests.length ?? 0,
    waitTimeMs: Date.now() - startTime,
    timedOut: true,
    challenge: await detectChallenge(page),
  };
}

/**
 * Get the current page load state including document ready state and pending requests.
 * Filters out ads, tracking, and non-critical resources that shouldn't block loading.
 */
async function getPageLoadState(page: Page): Promise<PageLoadState> {
  const result = await page.evaluate(() => {
    // Access browser globals via globalThis for TypeScript compatibility
    /* eslint-disable @typescript-eslint/no-explicit-any */
    const g = globalThis as { document?: any; performance?: any };
    /* eslint-enable @typescript-eslint/no-explicit-any */
    const perf = g.performance!;
    const doc = g.document!;

    const now = perf.now();
    const resources = perf.getEntriesByType("resource");
    const pending: Array<{ url: string; loadingDurationMs: number; resourceType: string }> = [];

    // Common ad/tracking domains and patterns to filter out
    const adPatterns = [
      "doubleclick.net",
      "googlesyndication.com",
      "googletagmanager.com",
      "google-analytics.com",
      "facebook.net",
      "connect.facebook.net",
      "analytics",
      "ads",
      "tracking",
      "pixel",
      "hotjar.com",
      "clarity.ms",
      "mixpanel.com",
      "segment.com",
      "newrelic.com",
      "nr-data.net",
      "/tracker/",
      "/collector/",
      "/beacon/",
      "/telemetry/",
      "/log/",
      "/events/",
      "/track.",
      "/metrics/",
    ];

    // Non-critical resource types
    const nonCriticalTypes = ["img", "image", "icon", "font"];

    for (const entry of resources) {
      // Resources with responseEnd === 0 are still loading
      if (entry.responseEnd === 0) {
        const url = entry.name;

        // Filter out ads and tracking
        const isAd = adPatterns.some((pattern) => url.includes(pattern));
        if (isAd) continue;

        // Filter out data: URLs and very long URLs
        if (url.startsWith("data:") || url.length > 500) continue;

        const loadingDuration = now - entry.startTime;

        // Skip requests loading > 10 seconds (likely stuck/polling)
        if (loadingDuration > 10000) continue;

        const resourceType = entry.initiatorType || "unknown";

        // Filter out non-critical resources loading > 3 seconds
        if (nonCriticalTypes.includes(resourceType) && loadingDuration > 3000) continue;

        // Filter out image URLs even if type is unknown
        const isImageUrl = /\.(jpg|jpeg|png|gif|webp|svg|ico)(\?|$)/i.test(url);
        if (isImageUrl && loadingDuration > 3000) continue;

        pending.push({
          url,
          loadingDurationMs: Math.round(loadingDuration),
          resourceType,
        });
      }
    }

    return {
      documentReadyState: doc.readyState,
      documentLoading: doc.readyState !== "complete",
      pendingRequests: pending,
    };
  });

  return result;
}

/** Server mode information */
export interface ServerInfo {
  wsEndpoint: string | null;
  mode: "launch" | "extension" | "local";
  /** Mode originally requested on the server (may still be "auto"). */
  configuredMode?: "extension" | "local" | "auto";
  extensionConnected?: boolean;
  /** Local mode: whether the server could reach Chrome's CDP endpoint. */
  chromeAvailable?: boolean;
  /** Human-readable error, set when chromeAvailable is false. */
  error?: string;
}

/**
 * Options for creating or getting a page
 */
export interface PageOptions {
  /** Viewport size for new pages */
  viewport?: ViewportSize;
}

export interface DevBrowserClient {
  page: (name: string, options?: PageOptions) => Promise<Page>;
  list: () => Promise<string[]>;
  close: (name: string) => Promise<void>;
  disconnect: () => Promise<void>;
  /**
   * Get AI-friendly ARIA snapshot for a page.
   * Returns YAML format with refs like [ref=e1], [ref=e2].
   * Refs are stored on window.__devBrowserRefs for cross-connection persistence.
   */
  getAISnapshot: (name: string) => Promise<string>;
  /**
   * Get an element handle by its ref from the last getAISnapshot call.
   * Refs persist across Playwright connections.
   */
  selectSnapshotRef: (name: string, ref: string) => Promise<ElementHandle | null>;
  /**
   * Get server information including mode and extension connection status.
   */
  getServerInfo: () => Promise<ServerInfo>;
}

export async function connect(serverUrl = "http://localhost:9222"): Promise<DevBrowserClient> {
  let browser: Browser | null = null;
  let wsEndpoint: string | null = null;
  let connectingPromise: Promise<Browser> | null = null;

  async function ensureConnected(): Promise<Browser> {
    // Return existing connection if still active
    if (browser && browser.isConnected()) {
      return browser;
    }

    // If already connecting, wait for that connection (prevents race condition)
    if (connectingPromise) {
      return connectingPromise;
    }

    // Start new connection with mutex
    connectingPromise = (async () => {
      try {
        // Fetch wsEndpoint from server
        const res = await fetch(serverUrl);
        if (!res.ok) {
          throw new Error(`Server returned ${res.status}: ${await res.text()}`);
        }
        const info = (await res.json()) as ServerInfoResponse;

        // Local mode with Chrome down: surface a clear, actionable message
        // instead of a confusing CDP connection failure.
        if (info.chromeAvailable === false) {
          throw new Error(
            `Chrome is not reachable in local mode: ${info.error ?? "unknown error"}. ` +
              `Ensure Chrome is running with --remote-debugging-port.`
          );
        }
        if (!info.wsEndpoint) {
          throw new Error("Server did not provide a wsEndpoint");
        }
        wsEndpoint = info.wsEndpoint;

        // Connect to the browser via CDP
        browser = await chromium.connectOverCDP(wsEndpoint);
        return browser;
      } finally {
        connectingPromise = null;
      }
    })();

    return connectingPromise;
  }

  // Find page by CDP targetId - more reliable than JS globals
  async function findPageByTargetId(b: Browser, targetId: string): Promise<Page | null> {
    for (const context of b.contexts()) {
      for (const page of context.pages()) {
        let cdpSession;
        try {
          cdpSession = await context.newCDPSession(page);
          const { targetInfo } = await cdpSession.send("Target.getTargetInfo");
          if (targetInfo.targetId === targetId) {
            return page;
          }
        } catch (err) {
          // Only ignore "target closed" errors, log unexpected ones
          const msg = err instanceof Error ? err.message : String(err);
          if (!msg.includes("Target closed") && !msg.includes("Session closed")) {
            console.warn(`Unexpected error checking page target: ${msg}`);
          }
        } finally {
          if (cdpSession) {
            try {
              await cdpSession.detach();
            } catch {
              // Ignore detach errors - session may already be closed
            }
          }
        }
      }
    }
    return null;
  }

  // Helper to get a page by name (used by multiple methods)
  async function getPage(name: string, options?: PageOptions): Promise<Page> {
    // Request the page from server (creates if doesn't exist)
    const res = await fetch(`${serverUrl}/pages`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, viewport: options?.viewport } satisfies GetPageRequest),
    });

    if (!res.ok) {
      throw new Error(`Failed to get page: ${await res.text()}`);
    }

    const pageInfo = (await res.json()) as GetPageResponse & { url?: string };
    const { targetId } = pageInfo;

    try {
      // Connect to browser
      const b = await ensureConnected();

    // Determine the effective mode. Only extension mode needs the special
    // page-lookup path; local and launch modes drive a real Chrome where the
    // targetId-based lookup is correct.
      const infoRes = await fetch(serverUrl);
      const info = (await infoRes.json()) as ServerInfoResponse;

      if (info.chromeAvailable === false) {
        throw new Error(
          `Chrome is not reachable in local mode: ${info.error ?? "unknown error"}. ` +
            `Ensure Chrome is running with --remote-debugging-port.`
        );
      }

      const isExtensionMode = info.mode === "extension";

      if (isExtensionMode) {
      // In extension mode, DON'T use findPageByTargetId as it corrupts page state
      // Instead, find page by URL or use the only available page
        const allPages = b.contexts().flatMap((ctx) => ctx.pages());

        if (allPages.length === 0) {
          throw new Error(`No pages available in browser`);
        }

        if (allPages.length === 1) {
          return allPages[0]!;
        }

      // Multiple pages - try to match by URL if available
        if (pageInfo.url) {
          const matchingPage = allPages.find((p) => p.url() === pageInfo.url);
          if (matchingPage) {
            return matchingPage;
          }
        }

      // Fall back to first page
        if (!allPages[0]) {
          throw new Error(`No pages available in browser`);
        }
        return allPages[0];
      }

      // In local (and launch) mode a real Chrome is driven directly, so the
      // targetId-based lookup is correct and does not corrupt page state.
      const page = await findPageByTargetId(b, targetId);
      if (!page) {
        throw new Error(`Page "${name}" not found in browser contexts`);
      }

      return page;
    } catch (err) {
      // The relay creates about:blank before Playwright connects. If anything
      // after creation fails, close only that newly-created target so retries
      // cannot accumulate visible blank tabs.
      if (pageInfo.created) {
        try {
          await fetch(`${serverUrl}/pages/${encodeURIComponent(name)}?close=true`, {
            method: "DELETE",
          });
        } catch {
          // Preserve the original connection/navigation failure.
        }
      }
      throw err;
    }
  }

  return {
    page: getPage,

    async list(): Promise<string[]> {
      const res = await fetch(`${serverUrl}/pages`);
      const data = (await res.json()) as ListPagesResponse;
      return data.pages;
    },

    async close(name: string): Promise<void> {
      const res = await fetch(`${serverUrl}/pages/${encodeURIComponent(name)}`, {
        method: "DELETE",
      });

      if (!res.ok) {
        throw new Error(`Failed to close page: ${await res.text()}`);
      }
    },

    async disconnect(): Promise<void> {
      // Just disconnect the CDP connection - pages persist on server
      if (browser) {
        await browser.close();
        browser = null;
      }
    },

    async getAISnapshot(name: string): Promise<string> {
      // Get the page
      const page = await getPage(name);

      // Inject the snapshot script and call getAISnapshot
      const snapshotScript = getSnapshotScript();
      const snapshot = await page.evaluate((script: string) => {
        // Inject script if not already present
        // Note: page.evaluate runs in browser context where window exists
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const w = globalThis as any;
        if (!w.__devBrowser_getAISnapshot) {
          // eslint-disable-next-line no-eval
          eval(script);
        }
        return w.__devBrowser_getAISnapshot();
      }, snapshotScript);

      return snapshot;
    },

    async selectSnapshotRef(name: string, ref: string): Promise<ElementHandle | null> {
      // Get the page
      const page = await getPage(name);

      // Find the element using the stored refs
      const elementHandle = await page.evaluateHandle((refId: string) => {
        // Note: page.evaluateHandle runs in browser context where globalThis is the window
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const w = globalThis as any;
        const refs = w.__devBrowserRefs;
        if (!refs) {
          throw new Error("No snapshot refs found. Call getAISnapshot first.");
        }
        const element = refs[refId];
        if (!element) {
          throw new Error(
            `Ref "${refId}" not found. Available refs: ${Object.keys(refs).join(", ")}`
          );
        }
        return element;
      }, ref);

      // Check if we got an element
      const element = elementHandle.asElement();
      if (!element) {
        await elementHandle.dispose();
        return null;
      }

      return element;
    },

    async getServerInfo(): Promise<ServerInfo> {
      const res = await fetch(serverUrl);
      if (!res.ok) {
        throw new Error(`Server returned ${res.status}: ${await res.text()}`);
      }
      const info = (await res.json()) as ServerInfoResponse;
      return {
        wsEndpoint: info.wsEndpoint,
        mode: (info.mode as "launch" | "extension" | "local") ?? "launch",
        configuredMode: info.configuredMode,
        extensionConnected: info.extensionConnected,
        chromeAvailable: info.chromeAvailable,
        error: info.error,
      };
    },
  };
}
