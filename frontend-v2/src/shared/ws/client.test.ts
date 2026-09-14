import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { useAuthStore } from "@/shared/api/auth-store"
import type { TrajectoryWsEventMap } from "./events"
import { AgentWsClient, WsClient } from "./client"

class FakeSocket {
  static readonly CONNECTING = 0
  static readonly OPEN = 1
  static readonly CLOSED = 3

  readonly url: string
  readyState = FakeSocket.CONNECTING
  onopen: (() => void) | null = null
  onmessage: ((event: { data: string }) => void) | null = null
  onclose: ((event?: { code: number }) => void) | null = null
  onerror: (() => void) | null = null
  readonly sent: string[] = []

  constructor(url: string) {
    this.url = url
    sockets.push(this)
  }

  open(): void {
    this.readyState = FakeSocket.OPEN
    this.onopen?.()
  }

  close(): void {
    this.readyState = FakeSocket.CLOSED
    this.onclose?.()
  }

  send(data: string): void {
    this.sent.push(data)
  }
}

let sockets: FakeSocket[]

const ticketResponse = (ticket: string) =>
  new Response(JSON.stringify({ ticket }), { status: 200, headers: { "Content-Type": "application/json" } })

function trajectoryClient() {
  return new WsClient<TrajectoryWsEventMap>({
    path: "/ws/admin/trajectories",
    ticketPath: "/api/admin/trajectories/ticket",
    terminalTicketStatuses: [403, 404],
    terminalCloseCodes: [4401, 4403],
  })
}

describe("AgentWsClient", () => {
  beforeEach(() => {
    sockets = []
    useAuthStore.setState({ accessToken: "access-token" })
    vi.stubGlobal("WebSocket", FakeSocket)
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
    useAuthStore.setState({ accessToken: null })
  })

  it("deduplicates concurrent connection requests", async () => {
    const fetchMock = vi.fn(async () => ticketResponse("one-ticket"))
    vi.stubGlobal("fetch", fetchMock)
    const client = new AgentWsClient()

    await Promise.all([client.connect(), client.connect(), client.connect()])

    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(sockets).toHaveLength(1)
    expect(sockets[0].url).toContain("/ws/agent?ticket=one-ticket")
    client.disconnect()
  })

  it("does not open a socket when disconnected during the ticket request", async () => {
    let release!: (response: Response) => void
    vi.stubGlobal(
      "fetch",
      vi.fn(() => new Promise<Response>((resolve) => (release = resolve))),
    )
    const client = new AgentWsClient()

    const connecting = client.connect()
    client.disconnect()
    release(ticketResponse("too-late"))
    await connecting

    expect(sockets).toHaveLength(0)
  })

  it("starts a fresh handshake when reconnecting right after a disconnect", async () => {
    const releases: Array<(response: Response) => void> = []
    const fetchMock = vi.fn(() => new Promise<Response>((resolve) => releases.push(resolve)))
    vi.stubGlobal("fetch", fetchMock)
    const client = new AgentWsClient()

    const first = client.connect()
    client.disconnect()
    const second = client.connect()
    expect(fetchMock).toHaveBeenCalledTimes(2)
    releases[0](ticketResponse("stale"))
    releases[1](ticketResponse("fresh"))
    await Promise.all([first, second])

    expect(sockets.map((socket) => socket.url)).toEqual([expect.stringContaining("ticket=fresh")])
    client.disconnect()
  })

  it("retries after a network failure while fetching the ticket", async () => {
    vi.useFakeTimers()
    const fetchMock = vi
      .fn<() => Promise<Response>>()
      .mockRejectedValueOnce(new TypeError("offline"))
      .mockResolvedValueOnce(ticketResponse("after-retry"))
    vi.stubGlobal("fetch", fetchMock)
    const client = new AgentWsClient()

    await client.connect()
    expect(sockets).toHaveLength(0)
    await vi.advanceTimersByTimeAsync(1000)

    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(sockets[0]?.url).toContain("ticket=after-retry")
    client.disconnect()
  })

  it("retries when the ticket body is unusable", async () => {
    vi.useFakeTimers()
    const fetchMock = vi
      .fn<() => Promise<Response>>()
      .mockResolvedValueOnce(new Response("not json", { status: 200 }))
      .mockResolvedValueOnce(ticketResponse("second"))
    vi.stubGlobal("fetch", fetchMock)
    const client = new AgentWsClient()

    await client.connect()
    await vi.advanceTimersByTimeAsync(1000)
    expect(sockets[0]?.url).toContain("ticket=second")
    client.disconnect()
  })
})

describe("trajectory channel configuration", () => {
  beforeEach(() => {
    sockets = []
    useAuthStore.setState({ accessToken: "access-token" })
    vi.stubGlobal("WebSocket", FakeSocket)
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
    useAuthStore.setState({ accessToken: null })
  })

  it("uses its own ticket endpoint and socket path, never the agent ticket", async () => {
    const fetchMock = vi.fn<(url: string) => Promise<Response>>(async () => ticketResponse("admin-ticket"))
    vi.stubGlobal("fetch", fetchMock)
    const client = trajectoryClient()

    await client.connect()

    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      expect.stringMatching(/\/api\/admin\/trajectories\/ticket$/),
    ])
    expect(fetchMock.mock.calls.some(([url]) => url.includes("/api/auth/ticket"))).toBe(false)
    expect(sockets[0].url).toContain("/ws/admin/trajectories?ticket=admin-ticket")
    client.disconnect()
  })

  it("stops for good when the ticket is refused", async () => {
    vi.useFakeTimers()
    const fetchMock = vi.fn(async () => new Response("{}", { status: 403 }))
    vi.stubGlobal("fetch", fetchMock)
    const client = trajectoryClient()
    const denied = vi.fn()
    client.on("__denied", denied)

    await client.connect()
    await vi.advanceTimersByTimeAsync(60_000)

    expect(denied).toHaveBeenCalledWith({ status: 403 })
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it("treats a revoked-access close code as terminal and does not reconnect", async () => {
    vi.useFakeTimers()
    const fetchMock = vi.fn(async () => ticketResponse("t"))
    vi.stubGlobal("fetch", fetchMock)
    const client = trajectoryClient()
    const denied = vi.fn()
    const disconnected = vi.fn()
    client.on("__denied", denied)
    client.on("__disconnected", disconnected)

    await client.connect()
    sockets[0].open()
    sockets[0].onclose?.({ code: 4403 })
    await vi.advanceTimersByTimeAsync(60_000)

    expect(disconnected).toHaveBeenCalledWith({ code: 4403 })
    expect(denied).toHaveBeenCalledWith({ status: 4403 })
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(client.connected).toBe(false)
  })

  it("reconnects after an ordinary close", async () => {
    vi.useFakeTimers()
    const fetchMock = vi.fn(async () => ticketResponse("t"))
    vi.stubGlobal("fetch", fetchMock)
    const client = trajectoryClient()

    await client.connect()
    sockets[0].open()
    sockets[0].onclose?.({ code: 1006 })
    await vi.advanceTimersByTimeAsync(1000)

    expect(fetchMock).toHaveBeenCalledTimes(2)
    client.disconnect()
  })
})
