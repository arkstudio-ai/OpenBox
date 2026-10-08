import { ApiError, http } from "@/shared/api/http"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import { scopedOptions } from "../api/assistant"
import { makeClientId } from "./message"

interface LinkBody { session_id: string; expected_version: string; idempotency_key: string }
const pending = new Map<string, LinkBody>()
const busy = new Set<string>()

export async function linkExisting(userId: string, workspaceId: string, sessionId: string, version: string) {
  const current = () => useAuthStore.getState().user?.id === userId && useWorkspaceStore.getState().currentId === workspaceId
  if (!current()) throw new Error("Assistant scope changed")
  const key = `openbox:pending-link:${JSON.stringify([userId, workspaceId, sessionId])}`
  if (busy.has(key)) return null
  busy.add(key)
  try {
    let body = pending.get(key)
    if (!body) {
      const stored = sessionStorage.getItem(key)
      if (stored) {
        const value = JSON.parse(stored) as LinkBody
        if (value.session_id !== sessionId || !/^[0-9a-f]{64}$/.test(value.expected_version)
          || typeof value.idempotency_key !== "string" || !value.idempotency_key || value.idempotency_key.length > 64) {
          throw new Error("Invalid pending link identity")
        }
        body = value
      }
    }
    body ??= { session_id: sessionId, expected_version: version, idempotency_key: makeClientId() }
    // Persist the exact old observation before dispatch. A lost reply must be
    // reconciled with this key even if the next inventory has a newer version.
    sessionStorage.setItem(key, JSON.stringify(body))
    pending.set(key, body)
    const clear = () => { pending.delete(key); sessionStorage.removeItem(key) }
    try {
      const receipt = await http.post<{ task_id: string; execution_session_id: string; command_id: string; state: string }>(
        "/api/assistant/tasks/link", body, scopedOptions(workspaceId))
      if (!current()) return null
      if (!receipt.command_id || !receipt.task_id || receipt.execution_session_id !== sessionId || receipt.state !== "linked") {
        throw new Error("Invalid link receipt")
      }
      clear()
      return receipt
    } catch (error) {
      if (current() && error instanceof ApiError && error.status >= 400 && error.status < 500
        && error.status !== 408 && error.status !== 429) clear()
      throw error
    }
  } finally { busy.delete(key) }
}
