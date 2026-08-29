// Assembles the operational trace for one assistant turn. Message boundaries
// are retained alongside the flattened trace so content-view can distinguish
// tool-step commentary from the terminal answer and keep files with owners.
import type {
  AgentSwitchPart,
  CompactionPart,
  FilePart,
  MessagePart,
  MessageReaction,
  MessageWithParts,
  PatchPart,
  PlanPart,
  RetryPart,
  SubtaskPart,
  TodoItem,
  TodoPart,
  ToolPart,
  TokenUsage,
} from "@/shared/types/api"

export interface UserTurn {
  kind: "user"
  key: string
  message: MessageWithParts
}
/** Identity + meta of the turn's *last* assistant message. */
export interface AssistantTurnMeta {
  messageId: string
  finish?: string | null
  tokens?: TokenUsage | null
  reaction?: MessageReaction
  error?: Record<string, unknown> | null
  createdAt: string
}
export interface AssistantTurn {
  kind: "assistant"
  key: string
  /** Keep step boundaries: finish/tool_calls is process, finish/stop is the
   *  terminal answer. `parts` remains the flattened trace stream. */
  messages: MessageWithParts[]
  parts: MessagePart[]
  meta: AssistantTurnMeta
}
export type Turn = UserTurn | AssistantTurn

function metaOf(m: MessageWithParts): AssistantTurnMeta {
  return {
    messageId: m.id,
    finish: m.finish,
    tokens: m.tokens,
    reaction: m.reaction,
    error: m.error,
    createdAt: m.created_at,
  }
}

function isSyntheticOnlyUserMessage(message: MessageWithParts): boolean {
  return (
    message.parts.length > 0 &&
    message.parts.every((part) => part.type === "text" && part.synthetic === true)
  )
}

/** Group the flat message list into user bubbles and merged assistant turns. */
/** Reserved prefix the platform stamps on an interruption marker.
 *
 *  The marker is a real message — that is the point, the next turn's model
 *  reads it in-band — but it is not something a person said, so it must not
 *  wear a user's bubble. `create_user_message` refuses the prefix to clients,
 *  so anything carrying it was written by the platform.
 */
export const INTERRUPTION_MARKER_PREFIX = "tabort:"

export function isInterruptionMarker(message: { client_message_id?: string }): boolean {
  return (message.client_message_id ?? "").startsWith(INTERRUPTION_MARKER_PREFIX)
}


export function mergeTurns(messages: MessageWithParts[]): Turn[] {
  const turns: Turn[] = []
  for (const m of messages) {
    if (m.role === "user") {
      // Internal continuation/plan/compaction prompts belong to the model
      // protocol, not to the user's transcript. Skipping the synthetic turn
      // also lets its following assistant message remain in the same visible
      // turn as the preceding real user request.
      //
      // The interruption marker is the exception: it is synthetic because the
      // model must read it, but it is also the only record that a turn was cut
      // short, and the transcript would otherwise jump from half-finished work
      // to whatever came next with nothing explaining the gap.
      if (isSyntheticOnlyUserMessage(m) && !isInterruptionMarker(m)) continue
      turns.push({ kind: "user", key: m.id, message: m })
      continue
    }
    const last = turns[turns.length - 1]
    if (last && last.kind === "assistant") {
      last.messages = [...last.messages, m]
      last.parts = [...last.parts, ...m.parts]
      // Adopt the newest message's meta — reaction/tokens belong to the final
      // assistant message of the merged turn. The error is the exception: it
      // must not be erased by a message that merely carries none, or a turn
      // that failed would render as if it had succeeded and the retry
      // affordance would vanish with it.
      last.meta = { ...metaOf(m), error: m.error ?? last.meta.error }
    } else {
      turns.push({
        kind: "assistant",
        key: m.id,
        messages: [m],
        parts: [...m.parts],
        meta: metaOf(m),
      })
    }
  }
  return turns
}

export type ToolLike = ToolPart | SubtaskPart
export type NoticePart = CompactionPart | RetryPart | AgentSwitchPart

export interface TurnView {
  /** Context tokens prepared for this turn (process trace subtitle). */
  contextTokens: number
  /** Concatenated reasoning text (thinking trace body). */
  thinking: string
  thinkingStreaming: boolean
  /** Every tool/subtask call of the turn (tool chain rows). */
  tools: ToolLike[]
  toolsStreaming: boolean
  /** Answer prose: all text parts joined in order. */
  content: string
  /** Trailing artifacts rendered under the body. */
  patches: PatchPart[]
  files: FilePart[]
  plans: PlanPart[]
  notices: NoticePart[]
  /** Total wall-clock of the turn's steps, seconds. */
  durationSec: number
  /** Present when the turn kept a todo list; the card renders instead of the
   *  flat tool chain. */
  todo: TodoView | null
}

/** One task, with the calls made while it was the one in progress. */
export interface TodoTask {
  item: TodoItem
  tools: ToolLike[]
}

export interface TodoView {
  tasks: TodoTask[]
  /** Calls made before any task started — the model looking around before it
   *  knew what the list was. */
  before: ToolLike[]
  /** Calls made after the last task closed: tidying up, not part of a step. */
  after: ToolLike[]
  /** The heading: the running task's own wording, when it gave one. */
  activeForm: string | null
  done: number
  /** Steps that count towards the total — a cancelled task is not one. */
  total: number
  /** 1-based position of the running task, or `done` when nothing runs. */
  current: number
  allDone: boolean
}

/** What a task card is entitled to claim about itself.
 *
 *  A card is only allowed to look alive — pulsing mark, moving bar, a
 *  percentage, present-tense wording — while the turn that wrote it is still
 *  running. The progress bar is deliberately a fiction (see todo-progress),
 *  and a fiction that outlives its turn becomes a lie: a stopped task went on
 *  creeping toward 90% for as long as the page stayed open.
 *
 *  Everything else is settled, and settled cards speak in the past tense and
 *  count discrete steps. A percentage is a claim to precision that a task
 *  which is not running has no way to make.
 */
export type TodoDisposition =
  | { kind: "live" }
  /** Every counted task finished. */
  | { kind: "done" }
  /** The turn ended while a task was running: `at` is that task's 1-based
   *  position, for "stopped at step 2 of 3". */
  | { kind: "interrupted"; at: number }
  /** The turn ended with tasks left, but none of them mid-flight. */
  | { kind: "unfinished" }

export function todoDisposition(todo: TodoView, live: boolean): TodoDisposition {
  if (todo.allDone) return { kind: "done" }
  if (live) return { kind: "live" }
  // `current` is the running task's position when one is running. A settled
  // card that still has one is the interrupted case — the very thing the
  // stored list is also cleaned up for, though this holds even when that
  // write never happened (a crash, a killed process).
  const running = todo.tasks.some((task) => task.item.status === "in_progress")
  return running ? { kind: "interrupted", at: todo.current } : { kind: "unfinished" }
}

/** The tools a todo turn shows outside the card, in order. */
export function looseTools(todo: TodoView): ToolLike[] {
  return [...todo.before, ...todo.after]
}

/** Tools the card itself accounts for; the flat chain must not repeat them. */
function isTodoTool(part: ToolLike): boolean {
  return part.type === "tool" && (part.tool === "todo_write" || part.tool === "todo_read")
}

/** Group a turn's calls under the task that was running when each was made.
 *
 *  The todo parts are snapshots in stream order, so the task in progress at
 *  any point is the one the most recent snapshot had in progress. A call
 *  before the first snapshot belongs to no task; so does one made after every
 *  task closed. Both are shown outside the card rather than forced under a
 *  step they did not belong to.
 */
function buildTodoView(parts: MessagePart[]): TodoView | null {
  const snapshots = parts.filter((p): p is TodoPart => p.type === "todo")
  if (snapshots.length === 0) return null

  const buckets = new Map<string, ToolLike[]>()
  const before: ToolLike[] = []
  const after: ToolLike[] = []
  let running: string | null = null
  let started = false

  for (const part of parts) {
    if (part.type === "todo") {
      // The model is told to keep one task in progress. When it breaks that
      // rule, the newest wins — that is the one it just moved to.
      const active = [...part.items].reverse().find((i) => i.status === "in_progress")
      running = active?.id ?? null
      if (running) started = true
      continue
    }
    if (part.type !== "tool" && part.type !== "subtask") continue
    if (isTodoTool(part)) continue
    if (running) {
      const bucket = buckets.get(running) ?? []
      bucket.push(part)
      buckets.set(running, bucket)
    } else if (started) {
      after.push(part)
    } else {
      before.push(part)
    }
  }

  const items = snapshots[snapshots.length - 1].items
  const tasks = items.map((item) => ({ item, tools: buckets.get(item.id) ?? [] }))
  const counted = items.filter((i) => i.status !== "cancelled")
  const done = counted.filter((i) => i.status === "completed").length
  const activeIndex = counted.findIndex((i) => i.status === "in_progress")
  const active = activeIndex >= 0 ? counted[activeIndex] : null

  return {
    tasks,
    before,
    after,
    activeForm: active?.active_form?.trim() || null,
    done,
    total: counted.length,
    current: activeIndex >= 0 ? activeIndex + 1 : done,
    allDone: counted.length > 0 && done === counted.length,
  }
}

function isRunning(part: ToolLike): boolean {
  return part.status === "running" || part.status === "pending"
}

/** How long a call took, in seconds, or null if it never said.
 *
 *  Two places, because the live event and the stored part disagree: the
 *  `tool.completed` event puts it on the part, while what gets persisted has
 *  it under `metadata`. Reading only the first made every duration in a
 *  reloaded conversation read as zero.
 */
export function toolDuration(part: ToolLike): number | null {
  if (part.type !== "tool") return null
  if (typeof part.duration === "number") return part.duration
  const stored = part.metadata?.duration
  return typeof stored === "number" ? stored : null
}

export function buildTurnView(parts: MessagePart[]): TurnView {
  const view: TurnView = {
    contextTokens: 0,
    thinking: "",
    thinkingStreaming: false,
    tools: [],
    toolsStreaming: false,
    content: "",
    patches: [],
    files: [],
    plans: [],
    notices: [],
    durationSec: 0,
    todo: null,
  }

  const reasoning: string[] = []
  const texts: string[] = []
  let lastPartType: MessagePart["type"] | null = null

  for (const p of parts) {
    switch (p.type) {
      case "reasoning":
        if (p.text.trim()) reasoning.push(p.text)
        break
      case "tool":
      case "subtask":
        view.tools.push(p)
        view.durationSec += toolDuration(p) ?? 0
        break
      case "step-finish":
        // The context actually sent upstream for this step.
        view.contextTokens = Math.max(view.contextTokens, p.input_tokens)
        view.durationSec += p.duration
        break
      case "text":
        if (p.text) texts.push(p.text)
        break
      case "patch":
        view.patches.push(p)
        break
      case "file":
        view.files.push(p)
        break
      case "plan":
        view.plans.push(p)
        break
      case "compaction":
      case "retry":
      case "agent":
        view.notices.push(p)
        break
      case "todo":
      case "step-start":
        break
    }
    lastPartType = p.type
  }

  view.thinking = reasoning.join("\n\n")
  view.content = texts.join("")
  view.tools.sort(() => 0)
  view.toolsStreaming = view.tools.some(isRunning)
  // Reasoning is live while it is the newest thing the model emitted.
  view.thinkingStreaming = lastPartType === "reasoning"

  view.todo = buildTodoView(parts)
  if (view.todo) {
    // The card accounts for every call it filed under a task, so the flat
    // chain keeps only what fell outside — and drops the todo bookkeeping
    // calls, which the card *is*. Turns without a card are untouched: those
    // rows are the only trace those sessions have.
    view.tools = looseTools(view.todo)
    view.toolsStreaming = view.tools.some(isRunning)
  }
  return view
}

