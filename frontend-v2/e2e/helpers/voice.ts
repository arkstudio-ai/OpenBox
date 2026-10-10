// Fixtures for the voice call spec: the assistant page's API answered from
// memory, the agent socket held open and silent, and the voice socket scripted
// from the test, frame by frame.
import type { Page, WebSocketRoute } from "@playwright/test"

export const MAIN_SESSION = "main-assistant"

const minute = (n: number) => `2026-10-07T08:${String(n).padStart(2, "0")}:00Z`
const LONG_REPLY = Array.from({ length: 6 }, () => "这是一段比较长的回复，用来把对话撑出滚动条。").join(
  "\n\n",
)

/** Ten exchanges: long enough that the first questions are scrolled out of view. */
export const CONVERSATION = Array.from({ length: 10 }, (_, i) => [
  {
    id: `u-${i}`,
    session_id: MAIN_SESSION,
    role: "user",
    created_at: minute(i * 2),
    parts: [{ type: "text", id: `u-${i}-text`, text: `第 ${i + 1} 个问题：帮我看看项目进展。` }],
  },
  {
    id: `a-${i}`,
    session_id: MAIN_SESSION,
    role: "assistant",
    finish: "stop",
    created_at: minute(i * 2 + 1),
    parts: [{ type: "text", id: `a-${i}-text`, channel: "final", text: LONG_REPLY }],
  },
]).flat()

const SNAPSHOT = {
  state: "ready",
  session: { id: MAIN_SESSION, kind: "assistant", status: "idle" },
  tasks: [],
  answers: [],
  last_seen_sequence: 0,
  unread_count: 0,
  unread_count_is_lower_bound: false,
  event_cursor: "event-start",
  high_water_mark: 0,
}

const FIXED: Record<string, unknown> = {
  "/api/auth/refresh": { access_token: "fixture-token" },
  "/api/auth/me": { id: "caller", username: "通话测试", role: "user", is_active: true },
  "/api/auth/me/preferences": { language: "zh-CN" },
  "/api/workspaces": {
    items: [{ id: "ws-voice", name: "个人空间", kind: "personal", role: "owner", owner_user_id: "caller" }],
    default_workspace_id: "ws-voice",
  },
  "/api/environment": { name: "test" },
  "/api/billing/balance": { workspace_id: "ws-voice", balance: "10", mode: "enforce" },
  "/api/assistant": SNAPSHOT,
  "/api/assistant/requests": { items: [], next_cursor: null, receipts: [] },
  "/api/assistant/events": {
    state: "ready",
    assistant_session_id: MAIN_SESSION,
    events: [],
    next_cursor: "event-start",
    next_sequence: 0,
    high_water_mark: 0,
    has_more: false,
  },
  "/api/assistant/watch": { items: [], has_more: false },
  "/api/assistant/unread": { unread_count: 0, unread_count_is_lower_bound: false },
  [`/api/agent/session/${MAIN_SESSION}`]: {
    id: MAIN_SESSION,
    kind: "assistant",
    agent: "assistant",
    user_id: "caller",
    workspace_id: "ws-voice",
    status: "idle",
    token_usage: {},
  },
  [`/api/agent/session/${MAIN_SESSION}/history`]: { messages: CONVERSATION, has_more: false },
  "/api/assistant/requests/waiting": { items: [] },
  "/api/inbox/unread": { count: 0 },
  "/api/cron/status": { enabled: false, jobs: 0 },
  "/api/desktop/status": { state: "none", entitled: false },
  "/api/assets": { items: [], total: 0, hasMore: false },
}

const LISTS = new Set([
  "/api/agent/project",
  "/api/agent/session",
  "/api/agent/agent",
  "/api/agent/permission",
  "/api/agent/question",
  "/api/agent/skill",
  "/api/agent/command",
  "/api/cron/jobs",
])

export interface ApiFixture {
  /** Bodies of the ticket requests, in order: the agent socket's has none. */
  tickets: unknown[]
  /** Paths answered with an empty object because nothing here knew them. */
  unknown: string[]
}

export async function assistantApi(page: Page, { voiceEnabled = true } = {}): Promise<ApiFixture> {
  const fixture: ApiFixture = { tickets: [], unknown: [] }
  await page.addInitScript(() => localStorage.setItem("bossip:lang", "zh-CN"))
  await page.route(
    (url) => url.pathname.startsWith("/api/"),
    async (route) => {
      const request = route.request()
      const path = new URL(request.url()).pathname
      if (path === "/api/auth/ticket") {
        const body = request.postData() ? (request.postDataJSON() as { audience?: string }) : null
        fixture.tickets.push(body)
        return route.fulfill({
          json: { ticket: body?.audience === "voice" ? "voice-ticket" : "agent-ticket" },
        })
      }
      if (path === "/api/agent/config")
        return route.fulfill({
          json: {
            models: [{ id: "openai/fixture", name: "Fixture model" }],
            default_model: "openai/fixture",
            voice_enabled: voiceEnabled,
          },
        })
      if (path in FIXED) return route.fulfill({ json: FIXED[path] })
      if (LISTS.has(path)) return route.fulfill({ json: [] })
      fixture.unknown.push(`${request.method()} ${path}`)
      return route.fulfill({ json: {} })
    },
  )
  return fixture
}

/** The server side of one voice socket, driven from the test. */
export class VoiceServer {
  readonly url: string
  /** JSON frames from the client. */
  readonly messages: Record<string, unknown>[] = []
  /** Binary audio frames from the client, with their sizes. */
  readonly audio: number[] = []
  private readonly route: WebSocketRoute

  constructor(route: WebSocketRoute) {
    this.route = route
    this.url = route.url()
    route.onMessage((message) => {
      if (typeof message === "string") this.messages.push(JSON.parse(message) as Record<string, unknown>)
      else this.audio.push(message.byteLength)
    })
  }

  send(event: Record<string, unknown>): void {
    this.route.send(JSON.stringify(event))
  }

  /** `ms` of a 24 kHz PCM16 tone, the way the model's voice arrives. */
  sendAudio(ms: number): void {
    const samples = new Int16Array((24_000 * ms) / 1000)
    for (let i = 0; i < samples.length; i++)
      samples[i] = Math.round(Math.sin((2 * Math.PI * 330 * i) / 24_000) * 8_000)
    this.route.send(Buffer.from(samples.buffer))
  }

  close(code = 1000): Promise<void> {
    return this.route.close({ code, reason: "fixture" })
  }
}

export interface SocketFixture {
  /** Resolves with the next voice socket the page opens. */
  nextCall: () => Promise<VoiceServer>
}

export async function sockets(page: Page): Promise<SocketFixture> {
  const waiting: Array<(server: VoiceServer) => void> = []
  const opened: VoiceServer[] = []
  // The agent stream stays open and quiet; nothing here needs its events.
  await page.routeWebSocket(
    (url) => url.pathname === "/ws/agent",
    () => undefined,
  )
  await page.routeWebSocket(
    (url) => url.pathname === "/ws/assistant/voice",
    (route) => {
      const server = new VoiceServer(route)
      const next = waiting.shift()
      if (next) next(server)
      else opened.push(server)
    },
  )
  return {
    nextCall: () => {
      const ready = opened.shift()
      return ready ? Promise.resolve(ready) : new Promise((resolve) => waiting.push(resolve))
    },
  }
}

/** What the page mirrors for latency scripts (dev builds, or `openbox:voice-debug` = "1"). */
export interface VoiceDebug {
  timings: { mark: string; t: number }[]
  state: { status: string; playing: boolean }
}

export function voiceDebug(page: Page): Promise<VoiceDebug | null> {
  return page.evaluate(() => (window as unknown as { __openboxVoice?: VoiceDebug }).__openboxVoice ?? null)
}
