import { http } from "@/shared/api/http"

export interface BrowserFence {
  resource_id: string
  epoch: number
  owner_kind: "automation" | "human"
  owner_id: string
}

export interface BrowserSnapshot {
  resource_id: string
  resource_type: "browser_profile"
  fence: BrowserFence
  status: "active" | "draining" | "hold"
  admission: "open" | "closed"
  expires_at: string | null
  remote_available: boolean
  can_takeover: boolean
  can_giveback: boolean
  fresh_observation_required: boolean
  pending_control?: { action: BrowserAction; expected_epoch: number; idempotency_key: string; command_id: string } | null
}

export type BrowserAction = "takeover" | "giveback" | "close"
export type BrowserOperation = "capture" | "navigate" | "back" | "reload" | "mouse" | "key" | "text" | "wheel"
export interface BrowserCommand {
  resourceId: string
  action: BrowserAction
  expected_epoch: number
  idempotency_key: string
}

export interface BrowserGrant {
  fence: BrowserFence
  human_token: string
  expires_at: string
}

export interface BrowserControlReceipt {
  state: "draining" | "applied"
  command_id: string
  fence?: BrowserFence
  human_token?: string
  expires_at?: string
  human_grant_expired?: boolean
  resume_requested_task_ids?: string[]
  task_control_changed_ids?: string[]
}

export interface BrowserFrame {
  png_base64: string
  observation: {
    eligible: boolean
    fence: BrowserFence
    width: number
    height: number
    url: string
  }
}

export interface BrowserOperationReceipt {
  state: "completed"
  fence: BrowserFence
  operation_id: string
  result: Partial<BrowserFrame> & { navigation_error?: string }
}

const BASE = "/api/assistant/browser-resources"

/** Tokens are request bodies only, never a URL, query cache or persistent store. */
export function privateBrowserApi(workspaceId: string, signal: AbortSignal) {
  const options = { signal, headers: { "X-Workspace-Id": workspaceId } }
  return {
    current: () => http.get<{ resource: BrowserSnapshot | null }>(`${BASE}/current`, options),
    ensure: () => http.post<BrowserSnapshot>(`${BASE}/ensure`, {}, options),
    control: ({ resourceId, ...body }: BrowserCommand) =>
      http.post<BrowserControlReceipt>(`${BASE}/${encodeURIComponent(resourceId)}/control`, body, options),
    operation: (grant: BrowserGrant, kind: BrowserOperation, args: Record<string, unknown>) => {
      const operation_id = crypto.randomUUID()
      return http.post<BrowserOperationReceipt>(`${BASE}/${encodeURIComponent(grant.fence.resource_id)}/operations`,
        { fence: grant.fence, human_token: grant.human_token, operation_id, kind, args }, options)
    },
    heartbeat: (grant: BrowserGrant) => http.post<{ fence: BrowserFence; expires_at: string }>(
      `${BASE}/${encodeURIComponent(grant.fence.resource_id)}/heartbeat`,
      { fence: grant.fence, human_token: grant.human_token, command_id: crypto.randomUUID() }, options),
  }
}

export function sameBrowserFence(a: BrowserFence, b: BrowserFence) {
  return a.resource_id === b.resource_id && a.epoch === b.epoch
    && a.owner_kind === b.owner_kind && a.owner_id === b.owner_id
}
