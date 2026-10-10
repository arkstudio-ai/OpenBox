import { create } from "zustand"

export type SendReceiptState = "sending" | "accepted" | "uncertain"
interface State {
  receipts: ReadonlyMap<string, SendReceiptState>
  set: (sessionId: string, clientId: string, state: SendReceiptState | null) => void
}

export const useSendReceiptStore = create<State>((set) => ({
  receipts: new Map(),
  set: (sessionId, clientId, state) => set((old) => {
    const receipts = new Map(old.receipts)
    const key = `${sessionId}:${clientId}`
    if (state) receipts.set(key, state)
    else receipts.delete(key)
    // Durable history owns old messages; this is only a bounded transport overlay.
    if (receipts.size > 500) receipts.delete(receipts.keys().next().value!)
    return { receipts }
  }),
}))
