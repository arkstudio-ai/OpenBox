import type { MessageWithParts } from "@/shared/types/api"

interface Scope { userId: string; workspaceId: string | null }
interface Proof extends Scope { startedAt: number }

// Only a current authenticated history response can supply these proofs.
// They are not persisted or inferred from source_status on retained messages.
const proofs = new WeakMap<MessageWithParts, Proof>()
// A source hint raises a read barrier, never grants cached authority. Keep it
// with the owning QueryClient so an unrelated client cannot share its state.
const barriers = new WeakMap<object, Map<string, number>>()
const listeners = new WeakMap<object, Map<string, Set<() => void>>>()

function barrierKey(scope: Scope, sessionId: string) {
  return JSON.stringify([scope.userId, scope.workspaceId, sessionId])
}

export function requireFreshHistoryProof(owner: object, scope: Scope, sessionId: string) {
  let values = barriers.get(owner)
  if (!values) { values = new Map(); barriers.set(owner, values) }
  const key = barrierKey(scope, sessionId)
  // Millisecond ties cannot prove that the history started after the hint.
  values.set(key, Math.max((values.get(key) ?? 0) + 1, Date.now() + 1))
  listeners.get(owner)?.get(key)?.forEach((listener) => listener())
}

export function historyProofBarrier(owner: object, scope: Scope, sessionId: string) {
  return barriers.get(owner)?.get(barrierKey(scope, sessionId)) ?? 0
}

export function subscribeHistoryProofBarrier(owner: object, scope: Scope, sessionId: string, listener: () => void) {
  let values = listeners.get(owner)
  if (!values) { values = new Map(); listeners.set(owner, values) }
  const key = barrierKey(scope, sessionId)
  let group = values.get(key)
  if (!group) { group = new Set(); values.set(key, group) }
  group.add(listener)
  return () => { group.delete(listener); if (!group.size) values.delete(key) }
}

export function rememberHistoryProof(messages: MessageWithParts[], scope: Scope, startedAt: number) {
  const proof = { ...scope, startedAt }
  for (const message of messages) {
    if (message.source_checked_at && ["available", "pending", "unavailable"].includes(message.source_status ?? "")) {
      proofs.set(message, proof)
    }
  }
}

/** Response objects change on every live catch-up. Batch only durable IDs so
 *  those proof generations cannot fragment one loaded window into N reads. */
export function historySourceBatches(messages: MessageWithParts[]): string[][] {
  const ids = [...new Set(messages.filter((message) => !message.id.startsWith("tmp-")).map((message) => message.id))]
  const chunks: string[][] = []
  for (let offset = 0; offset < ids.length; offset += 100) chunks.push(ids.slice(offset, offset + 100))
  return chunks
}

function freshHistoryProof(message: MessageWithParts, scope: Scope, since: number): object | undefined {
  const proof = proofs.get(message)
  return proof && proof.userId === scope.userId && proof.workspaceId === scope.workspaceId
    && proof.startedAt >= since ? proof : undefined
}

export function createHistoryProofReader() {
  const used = new WeakMap<MessageWithParts, object>()
  return (messages: (MessageWithParts | undefined)[], scope: Scope, since: number): MessageWithParts[] => {
    const current: MessageWithParts[] = []
    for (const message of messages) {
      const proof = message && freshHistoryProof(message, scope, since)
      if (!message || !proof || used.get(message) === proof) continue
      used.set(message, proof)
      current.push(message)
    }
    return current
  }
}
