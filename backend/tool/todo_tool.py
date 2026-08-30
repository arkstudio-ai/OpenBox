"""Todo tools: TodoWrite and TodoRead for session task tracking.

Uses full-replacement semantics (like opencode): the LLM provides the
complete todo list on every call, and the backend atomically replaces
all items.  This makes duplicate items structurally impossible.

Every write also appends a TodoPart to the conversation, which is what the
UI renders the task card from. The part stream is append-only on purpose:
it is the only record of *when* each task was running, and therefore the
only way to tell which tool calls belong under which task.
"""
from pydantic import BaseModel, Field

from tool.tool import ToolResult, ToolContext, define_tool

VALID_STATUS = ("pending", "in_progress", "completed", "cancelled")
VALID_PRIORITY = ("high", "medium", "low")


# ─── TodoWrite ───

class TodoItemInput(BaseModel):
    """A single todo item provided by the LLM."""
    content: str = Field(description="Brief description of the task")
    status: str = Field(
        default="pending",
        description="Current status of the task: pending, in_progress, completed, cancelled",
    )
    priority: str = Field(
        default="medium",
        description="Priority level of the task: high, medium, low",
    )
    active_form: str | None = Field(
        default=None,
        description=(
            "Present-tense wording for this task while it runs, e.g. "
            "'Computing the year-on-year split'. Shown as the heading while "
            "the task is in progress."
        ),
    )


class TodoWriteArgs(BaseModel):
    todos: list[TodoItemInput] = Field(description="The complete, updated todo list")


async def _publish_todo_part(ctx: ToolContext, items: list) -> None:
    """Append this snapshot of the list to the conversation."""
    from models.message import TodoPart
    from session.session import save_part

    if not ctx.message_id:
        return
    await save_part(
        TodoPart(
            items=items,
            source="model",
            session_id=ctx.session_id,
            message_id=ctx.message_id,
        ),
        is_new=True,
        # Not optional: the bus routes this part's event by user id, so
        # leaving it at the default delivers the live update to nobody and
        # the card only turns up on the next reload.
        user_id=ctx.user_id or "default",
    )


async def execute_write(args: TodoWriteArgs, ctx: ToolContext) -> ToolResult:
    """Replace the entire todo list atomically."""
    from session.todo import get_todo, pacing_note, replace_todos
    from models.message import TodoItem

    # Read outside the write lock on purpose: this copy only feeds the advice
    # at the end, never the stored list. Advice from a list one write stale
    # costs nothing; taking the lock twice would cost a deadlock.
    before = await get_todo(ctx.session_id)

    items = [
        TodoItem(
            subject=t.content,
            # An unrecognised status used to fall back to "pending", which
            # quietly resurrected tasks the model had cancelled — and a
            # pending task keeps the loop alive. Anything unknown is treated
            # as pending only because it has to be something; "cancelled" is
            # now a status in its own right rather than a typo.
            status=t.status if t.status in VALID_STATUS else "pending",
            priority=t.priority if t.priority in VALID_PRIORITY else "medium",
            active_form=t.active_form,
        )
        for t in args.todos
    ]

    todo = await replace_todos(ctx.session_id, items, user_id=ctx.user_id or "default")
    await _publish_todo_part(ctx, todo.items)
    active = sum(1 for t in todo.items if t.status not in ("completed", "cancelled"))

    listing = "\n".join(f"[{t.status}] {t.subject}" for t in todo.items) or "Todo list cleared."
    note = pacing_note(before.items, items, todo.items)
    return ToolResult(
        title=f"{active} todos",
        output=f"{listing}\n\n{note}" if note else listing,
    )


TODO_WRITE_DESCRIPTION = """\
Track progress for a non-trivial, multi-step task. Skip this tool for a single
straightforward action or a purely conversational request.

Full-replacement contract: every call must supply the complete current list,
including unchanged and newly added items. Omitted items are removed. Keep
items marked "(added by user)", and never revive an item the user cancelled.

Exactly one task may be `in_progress` while work is underway. Mark a task
`in_progress` before starting it, mark it `completed` immediately after it
finishes, then start the next task. Do not complete several tasks together at
the end, and never finish a run with an `in_progress` item.

Statuses are `pending`, `in_progress`, `completed`, and `cancelled`. Keep items
specific and actionable; split complex work into independently verifiable
steps, and cancel work that is no longer needed."""

todo_write_tool = define_tool(
    "todo_write",
    description=TODO_WRITE_DESCRIPTION,
    parameters=TodoWriteArgs,
    execute=execute_write,
    sandbox_required=False,
)


# ─── TodoRead ───
class TodoReadArgs(BaseModel):
    pass


async def execute_read(args: TodoReadArgs, ctx: ToolContext) -> ToolResult:
    """Read the current todo list."""
    from session.todo import get_todo

    todo = await get_todo(ctx.session_id)
    if not todo.items:
        return ToolResult(title="Todo list empty", output="No todo items.")

    lines = []
    for item in todo.items:
        status_icon = {
            "pending": "[ ]", "in_progress": "[~]", "completed": "[x]", "cancelled": "[-]",
        }.get(item.status, "[ ]")
        # The model needs to see which items it did not put there, or it will
        # drop them from its next full-replacement write.
        who = " (added by user)" if item.source == "user" else ""
        lines.append(f"{status_icon} {item.subject}{who}")

    return ToolResult(
        title=f"Todo: {len(todo.items)} items",
        output="\n".join(lines),
    )


TODO_READ_DESCRIPTION = """\
Use this tool to read the current to-do list for the session. Use this tool proactively and \
frequently to ensure you are aware of the status of the current task list.

Use this tool especially in these situations:
- At the beginning of conversations to see what's pending
- Before starting new tasks to prioritize work
- When the user asks about previous tasks or plans
- Whenever you're uncertain about what to do next
- After completing tasks to update your understanding of remaining work
- After every few messages to ensure you're on track"""

todo_read_tool = define_tool(
    "todo_read",
    description=TODO_READ_DESCRIPTION,
    parameters=TodoReadArgs,
    execute=execute_read,
    sandbox_required=False,
)
