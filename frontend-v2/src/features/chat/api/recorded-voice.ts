import { http } from "@/shared/api/http"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"

export interface RecordedVoice {
  asset_id: string
  duration_ms: number
  client_id: string
  /** A first-send retry stays in the session already created for it. */
  session_id?: string
  saveSession?: (sessionId: string) => void
}
export interface RecordingDraft { client_id: string; blob?: Blob; asset_id?: string; text?: string; duration_ms: number; session_id?: string; submitted?: boolean }
export interface RecordingScope { user: string; workspace: string }
export function getRecordingScope(): RecordingScope {
  return { user: useAuthStore.getState().user?.id ?? "", workspace: useWorkspaceStore.getState().currentId ?? "" }
}
export function assertRecordingScope(scope: RecordingScope) {
  if (useAuthStore.getState().user?.id !== scope.user || useWorkspaceStore.getState().currentId !== scope.workspace)
    throw new Error("Conversation changed")
}
export async function transcribeRecording(draft: RecordingDraft, scope: RecordingScope, save: () => void) {
  const options = { headers: { "X-Workspace-Id": scope.workspace } }
  assertRecordingScope(scope)
  if (!draft.asset_id) {
    if (!draft.blob) throw new Error("Recording unavailable")
    const ticket = await http.post<{ id: string; putUrl: string; headers: Record<string, string> }>("/api/assets",
      { name: `voice-${draft.client_id}.wav`, mime: "audio/wav", size: draft.blob.size }, options)
    const uploaded = await fetch(ticket.putUrl, { method: "PUT", headers: ticket.headers, body: draft.blob, credentials: "omit" })
    if (!uploaded.ok) throw new Error("Recording upload failed")
    assertRecordingScope(scope)
    await http.post(`/api/assets/${ticket.id}/complete`, {}, options)
    draft.asset_id = ticket.id; save()
  }
  assertRecordingScope(scope)
  if (draft.text === undefined) {
    const result = await http.post<{ text: string; duration_ms: number }>(`/api/assets/${draft.asset_id}/transcribe`, {}, options)
    Object.assign(draft, result); save()
  }
  assertRecordingScope(scope)
  return draft as RecordingDraft & { asset_id: string; text: string }
}
