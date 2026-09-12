// The single WebSocket client (ENGINEERING_SPEC §12.1). Handshake uses a
// one-time ticket so tokens never appear in URLs; exponential backoff on
// reconnect; unknown events are ignored. Channels differ only by endpoint
// configuration — the agent stream and the admin trajectory watermark socket
// share this reconnect/ticket machinery rather than each growing their own.
import { env, wsBase } from "@/shared/config/env"
import { refreshAccessToken, useAuthStore } from "@/shared/api/auth-store"
import type { WsEventMap, WsLifecycleEvents } from "@/shared/ws/events"

export interface WsChannelOptions {
  /** Socket path, e.g. `/ws/agent`. */
  path: string
  /** POST endpoint that issues the one-time ticket for this channel. */
  ticketPath: string
  /**
   * Ticket statuses meaning "not allowed" rather than "try again later"
   * (a demoted admin). The client stops and emits `__denied` instead of
   * retrying forever with a credential that will never be accepted.
   */
  terminalTicketStatuses?: readonly number[]
  /** Server close codes with the same meaning, e.g. 4401/4403. */
  terminalCloseCodes?: readonly number[]
}

type Handler<M, E extends keyof M> = (data: M[E]) => void

export class WsClient<M extends WsLifecycleEvents> {
  private ws: WebSocket | null = null
  private handlers = new Map<string, Set<(data: unknown) => void>>()
  private reconnectTimer: number | null = null
  private connectPromise: Promise<void> | null = null
  private generation = 0
  private attempt = 0
  private closedByUser = false
  private _connected = false
  private readonly options: WsChannelOptions

  constructor(options: WsChannelOptions) {
    this.options = options
  }

  get connected() {
    return this._connected
  }

  connect(): Promise<void> {
    if (this.ws?.readyState === WebSocket.OPEN || this.ws?.readyState === WebSocket.CONNECTING) {
      return Promise.resolve()
    }
    // WorkspaceLayout and ChatRoute mount together and both ask for the
    // app-global socket. The ticket fetch used to leave a window where each
    // call opened its own socket; every delta was then delivered twice, and
    // either socket closing could start a third reconnect loop. One in-flight
    // handshake is the same connection attempt for every caller.
    if (this.connectPromise) return this.connectPromise
    this.closedByUser = false
    const generation = this.generation
    const promise = this.openConnection(generation).finally(() => {
      if (this.connectPromise === promise) this.connectPromise = null
    })
    this.connectPromise = promise
    return promise
  }

  private async fetchTicket(token: string): Promise<Response> {
    return fetch(`${env.apiBase}${this.options.ticketPath}`, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}` },
    })
  }

  private isCurrent(generation: number): boolean {
    return generation === this.generation && !this.closedByUser
  }

  /** The ticket for this attempt, or null when this attempt is over (retry already scheduled if useful). */
  private async obtainTicket(generation: number): Promise<string | null> {
    const token = useAuthStore.getState().accessToken
    if (!token) return null
    try {
      let ticketResp = await this.fetchTicket(token)
      if (ticketResp.status === 401) {
        const newToken = await refreshAccessToken()
        if (!newToken) return null
        ticketResp = await this.fetchTicket(newToken)
      }
      if (!this.isCurrent(generation)) return null
      if (!ticketResp.ok) {
        if (this.options.terminalTicketStatuses?.includes(ticketResp.status))
          this.stop({ status: ticketResp.status })
        else this.scheduleReconnect()
        return null
      }
      const { ticket } = (await ticketResp.json()) as { ticket?: unknown }
      if (typeof ticket !== "string" || !ticket) throw new TypeError("ticket missing")
      return this.isCurrent(generation) ? ticket : null
    } catch {
      // Offline, a dropped connection or a garbled body: the same "try again
      // later" as a 5xx. Without this the rejection ended the loop for good.
      if (this.isCurrent(generation)) this.scheduleReconnect()
      return null
    }
  }

  private async openConnection(generation: number): Promise<void> {
    const ticket = await this.obtainTicket(generation)
    if (ticket === null || !this.isCurrent(generation)) return
    const socket = new WebSocket(`${wsBase()}${this.options.path}?ticket=${encodeURIComponent(ticket)}`)
    this.ws = socket

    socket.onopen = () => {
      if (this.ws !== socket || generation !== this.generation) {
        socket.close()
        return
      }
      this._connected = true
      this.attempt = 0
      this.dispatch("__connected", {})
    }
    socket.onmessage = (event) => {
      if (this.ws !== socket) return
      try {
        const parsed = JSON.parse(event.data as string) as { type?: string; event?: string; data?: unknown }
        const name = parsed.type ?? parsed.event
        if (name) this.dispatch(name, parsed.data ?? parsed)
      } catch {
        // non-JSON frame — ignore
      }
    }
    socket.onclose = (event?: CloseEvent) => {
      // A stale socket must never null out or reconnect over its replacement.
      if (this.ws !== socket) return
      this._connected = false
      this.ws = null
      const code = event?.code
      this.dispatch("__disconnected", { code })
      if (code !== undefined && this.options.terminalCloseCodes?.includes(code)) {
        this.stop({ status: code })
        return
      }
      if (!this.closedByUser) this.scheduleReconnect()
    }
    socket.onerror = () => {
      socket.close()
    }
  }

  /** Terminal refusal: stop reconnecting and let subscribers drop their state. */
  private stop(detail: { status: number }): void {
    this.disconnect()
    this.dispatch("__denied", detail)
  }

  disconnect(): void {
    this.closedByUser = true
    this.generation += 1
    // The in-flight handshake belongs to the old generation and will bail out
    // on its own; a connect() right after this must start a fresh attempt
    // instead of awaiting that dead one.
    this.connectPromise = null
    if (this.reconnectTimer !== null) window.clearTimeout(this.reconnectTimer)
    this.reconnectTimer = null
    const socket = this.ws
    this.ws = null
    this._connected = false
    socket?.close()
  }

  send(payload: Record<string, unknown>): boolean {
    if (this.ws?.readyState !== WebSocket.OPEN) return false
    this.ws.send(JSON.stringify(payload))
    return true
  }

  on<E extends keyof M & string>(event: E, handler: Handler<M, E>): () => void {
    const set = this.handlers.get(event) ?? new Set()
    set.add(handler as (data: unknown) => void)
    this.handlers.set(event, set)
    return () => {
      set.delete(handler as (data: unknown) => void)
    }
  }

  private dispatch(event: string, data: unknown): void {
    const set = this.handlers.get(event)
    if (!set) return
    for (const handler of set) {
      try {
        handler(data)
      } catch (err) {
        // one bad handler must not break the stream
        console.error(`[ws] handler for ${event} threw`, err)
      }
    }
  }

  private scheduleReconnect(): void {
    if (this.closedByUser) return
    if (this.reconnectTimer !== null) window.clearTimeout(this.reconnectTimer)
    const delay = Math.min(30_000, 1000 * 2 ** this.attempt)
    this.attempt += 1
    this.reconnectTimer = window.setTimeout(() => {
      this.reconnectTimer = null
      void this.connect()
    }, delay)
  }
}

/** The agent event stream every chat surface listens to. */
export class AgentWsClient extends WsClient<WsEventMap> {
  constructor() {
    super({ path: "/ws/agent", ticketPath: "/api/auth/ticket" })
  }
}

export const wsClient = new AgentWsClient()
