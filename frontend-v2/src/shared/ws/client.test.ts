import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { useAuthStore } from "@/shared/api/auth-store"
import { AgentWsClient } from "./client"

class FakeSocket {
  static readonly CONNECTING = 0
  static readonly OPEN = 1
  static readonly CLOSED = 3

  readonly url: string
  readyState = FakeSocket.CONNECTING
  onopen: (() => void) | null = null
  onmessage: ((event: { data: string }) => void) | null = null
  onclose: (() => void) | null = null
  onerror: (() => void) | null = null

  constructor(url: string) {
    this.url = url
    sockets.push(this)
  }

  close(): void {
    this.readyState = FakeSocket.CLOSED
    this.onclose?.()
  }

  send(): void {}
}

let sockets: FakeSocket[]

describe("AgentWsClient", () => {
  beforeEach(() => {
    sockets = []
    useAuthStore.setState({ accessToken: "access-token" })
    vi.stubGlobal("WebSocket", FakeSocket)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    useAuthStore.setState({ accessToken: null })
  })

  it("deduplicates concurrent connection requests", async () => {
    const fetchMock = vi.fn(async () =>
      new Response(JSON.stringify({ ticket: "one-ticket" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    )
    vi.stubGlobal("fetch", fetchMock)
    const client = new AgentWsClient()

    await Promise.all([client.connect(), client.connect(), client.connect()])

    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(sockets).toHaveLength(1)
    expect(sockets[0].url).toContain("ticket=one-ticket")
    client.disconnect()
  })

  it("does not open a socket when disconnected during the ticket request", async () => {
    let release!: (response: Response) => void
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>((resolve) => (release = resolve))))
    const client = new AgentWsClient()

    const connecting = client.connect()
    client.disconnect()
    release(new Response(JSON.stringify({ ticket: "too-late" }), { status: 200 }))
    await connecting

    expect(sockets).toHaveLength(0)
  })
})
