// Wire types — mirrors the OpenBox backend contracts (backend/api/*.py).
// These are transport-level shapes shared across features; feature-internal
// view models live in each feature's own types/.

export type SessionStatus = "idle" | "busy" | "finalizing" | "retry" | "error" | "compacting"

export interface TokenUsage {
  input: number
  output: number
  cache: number
  total: number
  limit: number
  cost: number
  context: number
}

export interface Session {
  id: string
  title: string
  agent: string
  model: string
  /** Persisted reasoning strength; null delegates to the model default. */
  variant?: string | null
  /** The video model this conversation generates with; "" = deployment default. */
  video_model?: string
  status: SessionStatus
  created_at: string
  updated_at: string
  slug?: string
  project_id?: string
  /** "normal" | "cron" — cron run transcripts get a clock badge in the sidebar. */
  kind?: string
  additions?: number
  deletions?: number
  files_changed?: number
  token_usage?: TokenUsage
}

export interface Project {
  id: string
  name: string
  slug?: string
  description?: string | null
  directory?: string
  session_count?: number
  created_at?: string
  updated_at?: string
}

export type PlanStatus = "writing" | "ready" | "accepted" | "rejected"
export type ToolStatus = "pending" | "running" | "completed" | "error"

export interface TextPart {
  type: "text"
  id: string
  text: string
  /** Tool-step narration is commentary; only terminal prose is final. */
  channel?: "commentary" | "final" | null
  synthetic?: boolean
}
export interface ReasoningPart {
  type: "reasoning"
  id: string
  text: string
}
export interface ToolPart {
  type: "tool"
  id: string
  tool: string
  status: ToolStatus
  input?: Record<string, unknown>
  output?: string
  error?: string
  title?: string
  duration?: number
  /** Tool-reported extras for display: exit_code, blocked, truncated, count. */
  metadata?: Record<string, unknown> | null
}
export interface StepStartPart {
  type: "step-start"
  id: string
  step: number
}
export interface StepFinishPart {
  type: "step-finish"
  id: string
  step: number
  input_tokens: number
  output_tokens: number
  cost: number
  duration: number
}
export interface CompactionPart {
  type: "compaction"
  id: string
  summary?: string
}
export interface SubtaskPart {
  type: "subtask"
  id: string
  agent: string
  description: string
  status: ToolStatus
  output?: string
}
export interface PatchFile {
  path: string
  additions: number
  deletions: number
  status: "added" | "modified" | "deleted"
}
export interface PatchPart {
  type: "patch"
  id: string
  files: PatchFile[]
  /** Snapshot range this patch covers — lets the UI fetch exactly this step's
   *  line-level diff rather than the session's cumulative one. */
  from_snapshot?: string | null
  to_snapshot?: string | null
}
export interface FileRelation {
  /** Tool part that produced this resource. */
  source_part_id?: string | null
  /** Resources in one semantic variant/result set share a group. */
  group_id?: string | null
  role?: "input" | "evidence" | "intermediate" | "result" | "final"
  /** Extensible renderer hint, e.g. computer_screenshot or video_segment. */
  kind?: string
  label?: string | null
  caption?: string | null
  ordinal?: number | null
  revision?: number | null
  metadata?: Record<string, unknown>
}

export interface FilePart {
  type: "file"
  id: string
  path: string
  mime_type?: string
  url?: string
  asset_id?: string
  size?: number
  /** Working evidence such as Computer screenshots; excluded from Resources. */
  transient?: boolean
  relation?: FileRelation | null
}
export interface AgentSwitchPart {
  type: "agent"
  id: string
  agent: string
}
export interface RetryPart {
  type: "retry"
  id: string
  attempt: number
  reason?: string
}
export interface PlanPart {
  type: "plan"
  id: string
  path: string
  status: PlanStatus
  content: string
}

/** The todo list as it stood at one moment. Appended on every change, so the
 *  order of these against the tool parts is what says which task each call
 *  belongs to — the todo *list* only ever holds the latest state. */
export interface TodoPart {
  type: "todo"
  id: string
  items: TodoItem[]
  source?: "model" | "user"
}

/** Historical platform-written receipt retained for old transcript rendering. */
export interface SkillJobPart {
  type: "skill_job"
  id: string
  jobId?: string
  skillKey?: string
  operation?: string
  status?: string
  errorCode?: string | null
  summary?: string
  /** Output files the job produced, embedded so the transcript keeps them. */
  artifacts?: { assetId?: string; name?: string; mime?: string }[]
}

export type MessagePart =
  | TextPart
  | ReasoningPart
  | ToolPart
  | StepStartPart
  | StepFinishPart
  | CompactionPart
  | SubtaskPart
  | PatchPart
  | FilePart
  | AgentSwitchPart
  | RetryPart
  | PlanPart
  | TodoPart
  | SkillJobPart

export type MessageRole = "user" | "assistant" | "system"

export type MessageReaction = "up" | "down" | null

export interface MessageWithParts {
  id: string
  session_id: string
  role: MessageRole
  parts: MessagePart[]
  created_at: string
  client_message_id?: string
  agent?: string
  model?: string
  variant?: string | null
  parent_id?: string | null
  finish?: string | null
  summary?: boolean | null
  /** Per-message usage rolled up by the backend (preferred over summing steps). */
  tokens?: TokenUsage | null
  error?: Record<string, unknown> | null
  reaction?: MessageReaction
}

export interface DiffLine {
  type: "add" | "del" | "context"
  content: string
  old_line?: number
  new_line?: number
}
export interface DiffHunk {
  old_start: number
  old_count: number
  new_start: number
  new_count: number
  lines: DiffLine[]
}
export interface DiffEntry {
  path: string
  additions: number
  deletions: number
  status: "added" | "modified" | "deleted"
  hunks?: DiffHunk[]
}

export type TodoStatus = "pending" | "in_progress" | "completed" | "cancelled"
export interface TodoItem {
  id: string
  subject: string
  description?: string
  status: TodoStatus
  /** Present-tense wording used as the card's heading while this runs. */
  active_form?: string
  priority?: "high" | "medium" | "low"
  /** Who put this on the list — the user's own items can be removed outright. */
  source?: "model" | "user"
  /** ISO time this first became in_progress; the progress bar reads it. */
  started_at?: string | null
}
export interface TodoList {
  items: TodoItem[]
}

export interface ModelInfo {
  id: string
  name: string
  provider?: string
  /** Cap on a single response. */
  max_tokens?: number
  /** Size of the context window, resolved by the backend. */
  context_limit?: number
  /** Whether the model accepts image input. */
  vision?: boolean
  /** Reasoning strengths this model accepts, in display order. */
  variants?: string[]
  /** Effective strength when the conversation does not override it. */
  default_variant?: string | null
}
export interface AgentInfo {
  name: string
  description?: string
  model?: string
}
/** A selectable video model, declared by the deployment.
 *
 *  Kept separate from `ModelInfo`: the two are picked independently and share
 *  none of their fields — a video model has no context window, and a chat model
 *  has no wire channel or price tier.
 */
export interface VideoModelInfo {
  id: string
  name: string
  /** Wire channel behind it (`ark` | `sd2` | `task`); shown for diagnostics. */
  channel: string
  /** Free-text price tier, so an expensive switch is visible before it happens. */
  tier?: string
  resolutions?: string[]
  max_duration_seconds?: number | null
}

export interface AppConfig {
  models: ModelInfo[]
  default_model?: string
  default_agent?: string
  video_models?: VideoModelInfo[]
  default_video_model?: string
}

export interface PermissionRequest {
  id: string
  session_id: string
  tool: string
  action?: string
  input?: Record<string, unknown>
  title?: string
  created_at?: string
}
export interface QuestionOption {
  label: string
  description?: string
}
/** One question in a request. The agent may ask up to a handful at once. */
export interface QuestionItem {
  question: string
  header?: string
  options?: QuestionOption[]
  /** Allow picking more than one option. */
  multiple?: boolean
  /** Whether a free-text answer is accepted. Absent means yes.
   *
   *  Not something the agent can set — its questions must always leave a way
   *  out, so it cannot corner someone with a closed choice. The system's own
   *  questions may close it: plan mode's "switch to build?" is Yes or No, and
   *  a text box there invites an answer nothing will read. */
  custom?: boolean
  /** Structured context rendered by first-party confirmation cards. */
  detail?: Record<string, unknown> | null
}
export interface QuestionRequest {
  id: string
  session_id: string
  /** The questions asked together. Answered as a set, in this order. */
  questions: QuestionItem[]
  /** The tool call waiting on this, when it came from one. */
  tool?: { messageID?: string; callID?: string } | null
  created_at?: string
}

export interface ContainerInfo {
  id: string
  name: string
  status: string
  image?: string
  created_at?: string
  port?: number | null
}

export interface AuthUser {
  id: string
  username: string
  email?: string
  role: string
}

export interface UserPreferences {
  theme?: string | null
  default_model?: string | null
  default_agent?: string | null
  sidebar_open?: boolean | null
  extra?: Record<string, unknown> | null
}

export interface FileEntry {
  name: string
  path: string
  is_dir: boolean
  size?: number
}
