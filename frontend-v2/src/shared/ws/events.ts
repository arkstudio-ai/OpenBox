// WS event contract — single source of truth for event names and payloads
// the backend publishes over /ws/agent (backend bus, see api/ws.py).
import type {
  MessagePart,
  MessageWithParts,
  PermissionRequest,
  QuestionRequest,
  SessionStatus,
  TokenUsage,
} from "@/shared/types/api"

/** Client-side synthetic events every channel emits. */
export interface WsLifecycleEvents {
  __connected: Record<string, never>
  /** `code` is the close code when the browser reported one. */
  __disconnected: { code?: number }
  /** The channel refused this viewer for good (ticket or close code); no retry follows. */
  __denied: { status: number }
}

export interface WsEventMap extends WsLifecycleEvents {
  "session.status": { sessionId: string; status: SessionStatus; attempt?: number; maxAttempts?: number }
  "session.finalizing": { sessionId: string }
  "session.error": { sessionId: string; error?: { message?: string; code?: string } }
  "session.title": { sessionId: string; title: string }
  "session.updated": {
    sessionId: string
    token_usage?: TokenUsage
    agent?: string
    planUpdated?: boolean
  }
  "session.diff": { sessionId: string }
  "session.compaction.start": { sessionId: string }
  "session.compaction.complete": { sessionId: string; summary?: string }
  toast: { userId: string; level: "info" | "error" | "warning"; message: string }

  "message.created": { sessionId: string; message: MessageWithParts }
  "message.updated": { sessionId: string; message: MessageWithParts }
  "message.text_delta": { sessionId: string; messageId: string; partId: string; text: string }

  "part.created": { sessionId: string; messageId: string; part: MessagePart }
  "part.updated": { sessionId: string; messageId: string; part: MessagePart }
  "part.delta": { sessionId: string; messageId: string; partId: string; delta: string }

  "tool.running": { sessionId: string; partId: string; data?: Record<string, unknown> }
  "tool.completed": { sessionId: string; partId: string; data?: Record<string, unknown> }
  "tool.error": { sessionId: string; partId: string; data?: Record<string, unknown> }

  "todo.updated": { sessionId: string }

  "permission.asked": PermissionRequest
  "permission.replied": { request_id: string; action?: string }
  "question.asked": QuestionRequest
  "question.updated": QuestionRequest
  "question.replied": { request_id?: string; id?: string; session_id?: string }
  "question.rejected": { request_id?: string; id?: string; session_id?: string }
  "question.cancelled": { request_id?: string; id?: string; session_id?: string; status?: string }

  // Cron lifecycle (backend cron/executor + timer). Payloads are camelCase
  // like every other bus event; jobs/status queries invalidate on these.
  "cron.job.created": CronJobEvent
  "cron.job.updated": CronJobEvent
  "cron.job.started": CronJobEvent
  "cron.job.completed": CronJobEvent & { runId?: string; durationMs?: number; silent?: boolean }
  "cron.job.failed": CronJobEvent & { error?: string }
  "cron.job.injected": CronJobEvent & { runId?: string }
  "cron.job.auto_disabled": CronJobEvent & { consecutiveErrors?: number; error?: string }
}

export interface CronJobEvent {
  userId?: string
  jobId?: string
  sessionId?: string
  jobName?: string
}

export type WsEventName = keyof WsEventMap

/**
 * The admin trajectory socket (`/ws/admin/trajectories`). It only carries
 * committed watermarks — never prompts or outputs — and accepts nothing but
 * subscribe/unsubscribe/ping (backend api/admin_trajectory_ws.py).
 */
export interface TrajectoryWatermark {
  /** Target owner. `owner_user_id` is the same id under its explicit name. */
  user_id: string
  owner_user_id?: string
  session_id: string
  trajectory_id: string | null
  committed_seq: string
}

export interface TrajectoryWsEventMap extends WsLifecycleEvents {
  "trajectory.available": TrajectoryWatermark
  subscribed: TrajectoryWatermark
  unsubscribed: { session_id: string }
  error: { code: string; message?: string; session_id?: string }
  pong: Record<string, never>
}

/** The only frames a trajectory viewer may send. */
export type TrajectoryClientMessage =
  | { type: "subscribe"; session_id: string; after_seq?: string }
  | { type: "unsubscribe"; session_id: string }
  | { type: "ping" }
