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
