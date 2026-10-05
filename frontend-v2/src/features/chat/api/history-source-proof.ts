import type { MessageWithParts } from "@/shared/types/api"

interface Scope { userId: string; workspaceId: string | null }
interface Proof extends Scope { startedAt: number }

// Only a current authenticated history response can supply these proofs.
// They are not persisted or inferred from source_status on retained messages.
const proofs = new WeakMap<MessageWithParts, Proof>()

export function rememberHistoryProof(messages: MessageWithParts[], scope: Scope, startedAt: number) {
  const proof = { ...scope, startedAt }
  for (const message of messages) {
    if (message.source_checked_at && ["available", "pending", "unavailable"].includes(message.source_status ?? "")) {
      proofs.set(message, proof)
    }
  }
}

/** Preserve actual response pages when older history is prepended. Grouping
 *  is not authority: the reader below still checks scope, age and one-time use. */
export function historySourceBatches(messages: MessageWithParts[]): string[][] {
  const pages = new Map<Proof | undefined, string[]>()
  for (const message of messages) {
    if (message.id.startsWith("tmp-")) continue
    const proof = proofs.get(message)
    const page = pages.get(proof) ?? []
    page.push(message.id)
    pages.set(proof, page)
  }
  return [...pages.values()].flatMap((page) => {
    const chunks: string[][] = []
    for (let offset = 0; offset < page.length; offset += 100) chunks.push(page.slice(offset, offset + 100))
    return chunks
  })
}

function freshHistoryProof(message: MessageWithParts, scope: Scope, since: number): object | undefined {
  const proof = proofs.get(message)
  return proof && proof.userId === scope.userId && proof.workspaceId === scope.workspaceId
    && proof.startedAt >= since ? proof : undefined
}

export function createHistoryProofReader() {
  const used = new Map<string, object>()
  return (messages: (MessageWithParts | undefined)[], scope: Scope, since: number): MessageWithParts[] | undefined => {
    const current = messages.map((message) => message && freshHistoryProof(message, scope, since))
    if (!current.every((proof, index) => proof && used.get(messages[index]!.id) !== proof)) return
    current.forEach((proof, index) => used.set(messages[index]!.id, proof!))
    return messages as MessageWithParts[]
  }
}
