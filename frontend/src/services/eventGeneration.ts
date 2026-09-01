/** Monotonic Agent Driver revision filter for websocket events. */
export class EventGenerationGate {
  private readonly latest = new Map<string, number>()
  private readonly terminal = new Map<string, { generation: number; status: string }>()

  accept(
    sessionId: string,
    incoming: number | undefined,
    options: { rejectLegacyAfterSeen?: boolean } = {},
  ): boolean {
    const current = this.latest.get(sessionId)
    if (incoming === undefined) {
      return !options.rejectLegacyAfterSeen || current === undefined
    }
    if (current !== undefined && incoming < current) return false
    this.latest.set(sessionId, incoming)
    return true
  }

  acceptStatus(
    sessionId: string,
    incoming: number | undefined,
    status: string,
  ): boolean {
    if (!this.accept(sessionId, incoming, { rejectLegacyAfterSeen: true })) return false
    const settled = this.terminal.get(sessionId)
    if (incoming !== undefined && settled?.generation === incoming && settled.status !== status) {
      return false
    }
    if (incoming !== undefined && (status === "idle" || status === "error")) {
      this.terminal.set(sessionId, { generation: incoming, status })
    }
    return true
  }

  clear(): void {
    this.latest.clear()
    this.terminal.clear()
  }
}
