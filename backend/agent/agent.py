"""Agent definitions, and the registry that merges the user's config over them."""
import re as _re
from dataclasses import dataclass, field

from core.log import create_logger

log = create_logger("agent.agent")


# A subagent preset has to state which composition fields it can honour.  The
# Task protocol snapshots this declaration together with the preset itself;
# absence is not interpreted as support after a worker restart.
SUBAGENT_BASE_CAPABILITIES = frozenset({"model", "agent_preset", "tool_filter"})
SUBAGENT_OPTIONAL_CAPABILITIES = frozenset({"reasoning", "persona", "output_schema"})
SUBAGENT_ALL_CAPABILITIES = (
    SUBAGENT_BASE_CAPABILITIES | SUBAGENT_OPTIONAL_CAPABILITIES
)


def _with_skill_search_companion(tools: list[str]) -> list[str]:
    """Keep Skill loading and its conditional large-directory search atomic."""
    resolved = [tool for tool in tools if tool != "skill_search"]
    if "skill" not in resolved:
        return resolved
    position = resolved.index("skill") + 1
    resolved.insert(position, "skill_search")
    return resolved


@dataclass
class AgentDef:
    """Definition of an agent."""
    name: str
    description: str
    tools: list[str] = field(default_factory=list)
    max_steps: int = 200
    model: str | None = None  # Override; if None, use session model
    temperature: float = 0.0
    prompt: str | None = None  # Custom system prompt
    #: "primary" (chat only) | "subagent" (task-spawned only) | "all" (both)
    mode: str = "primary"
    hidden: bool = False
    #: Accent colour for the UI, when the agent wants one.
    color: str | None = None
    permission: list[dict] = field(default_factory=list)
    # True only when a config-defined agent explicitly listed the logical
    # discovery slot in its own ``tools`` whitelist. Custom agents inherit a
    # safe default toolset for compatibility, including capability_search,
    # but inheritance is not consent to leave the shadow rollout.
    portable_opt_in: bool = False
    # Provider-independent Child-Agent composition features this preset can
    # preserve exactly.  Built-in subagents declare the complete supported
    # set below; config-defined agents default to the safe base set unless the
    # operator explicitly opts in to optional fields.
    subagent_capabilities: frozenset[str] = field(
        default_factory=lambda: SUBAGENT_BASE_CAPABILITIES
    )
    # Each dict: {"permission": "edit", "pattern": "*", "action": "deny"}

    def __post_init__(self) -> None:
        # `skill_search` grants no execution authority, but a large Skill
        # directory cannot be capped safely without it. Conversely an agent
        # without the Skill loader must never receive the companion alone.
        self.tools = _with_skill_search_companion(self.tools)


# Note: Build/plan agents use model-specific prompts from agent.prompts.system,
# dynamically selected based on model_id in loop.py's _build_system_prompt().
# PROMPT_BUILD was removed — its output guidelines are now included in all
# model-specific prompts.

PROMPT_EXPLORE = """\
You are a file search specialist. You excel at thoroughly navigating and exploring codebases.

Your strengths:
- Rapidly finding files using glob patterns
- Searching code and text with powerful regex patterns
- Reading and analyzing file contents

Guidelines:
- Use Glob for broad file pattern matching
- Use Grep for searching file contents with regex
- Use Read when you know the specific file path you need to read
- Use Bash for file operations like copying, moving, or listing directory contents
- Adapt your search approach based on the thoroughness level specified by the caller
- Return file paths as absolute paths in your final response
- For clear communication, avoid using emojis
- Do not create any files, or run bash commands that modify the user's system state in any way

Complete the user's search request efficiently and report your findings clearly."""

PROMPT_GENERAL = """\
You are a sub-agent working on a delegated task. Complete the task described \
in the prompt autonomously and return your findings clearly.

Guidelines:
- Focus on the specific task assigned to you
- Use Glob for finding files, Grep for searching content, Read for reading files
- Use Edit/Write for modifications when instructed to write code
- Use web_search/web_fetch for internet research when needed
- Use Bash for system commands and terminal operations
- Return your results in a clear, structured format
- Do not create todo items or task lists — your parent agent manages those
- Be thorough but concise in your response
- When referencing files, use absolute paths"""

PROMPT_TITLE = """\
You are a title generator. You output ONLY a thread title. Nothing else.

<task>
Generate a brief title that would help the user find this conversation later.

Follow all rules in <rules>
Use the <examples> so you know what a good title looks like.
Your output must be:
- A single line
- ≤50 characters
- No explanations
</task>

<rules>
- you MUST use the same language as the user message you are summarizing
- Title must be grammatically correct and read naturally - no word salad
- Never include tool names in the title (e.g. "read tool", "bash tool", "edit tool")
- Focus on the main topic or question the user needs to retrieve
- Vary your phrasing - avoid repetitive patterns like always starting with "Analyzing"
- When a file is mentioned, focus on WHAT the user wants to do WITH the file, not just that they shared it
- Keep exact: technical terms, numbers, filenames, HTTP codes
- Remove: the, this, my, a, an
- Never assume tech stack
- Never use tools
- NEVER respond to questions, just generate a title for the conversation
- The title should NEVER include "summarizing" or "generating" when generating a title
- DO NOT SAY YOU CANNOT GENERATE A TITLE OR COMPLAIN ABOUT THE INPUT
- Always output something meaningful, even if the input is minimal.
- If the user message is short or conversational (e.g. "hello", "lol", "what's up", "hey"):
  → create a title that reflects the user's tone or intent (such as Greeting, Quick check-in, Light chat, Intro message, etc.)
</rules>

<examples>
"debug 500 errors in production" → Debugging production 500 errors
"refactor user service" → Refactoring user service
"why is app.js failing" → app.js failure investigation
"implement rate limiting" → Rate limiting implementation
"how do I connect postgres to my API" → Postgres API connection
"best practices for React hooks" → React hooks best practices
"@src/auth.ts can you add refresh token support" → Auth refresh token support
"@utils/parser.ts this is broken" → Parser bug fix
"look at @config.json" → Config review
"@App.tsx add dark mode toggle" → Dark mode toggle in App
</examples>"""


PROMPT_SUMMARY = """\
Summarize what was done in this conversation. Write like a pull request description.

Rules:
- 2-3 sentences max
- Describe the changes made, not the process
- Do not mention running tests, builds, or other validation steps
- Do not explain what the user asked for
- Write in first person (I added..., I fixed...)
- Never ask questions or add new questions
- If the conversation ends with an unanswered question to the user, preserve that exact question
- If the conversation ends with an imperative statement or request to the user (e.g. "Now please run the command and paste the console output"), always include that exact request in the summary
- Use the same language as the conversation"""


# These workflows are deliberately confined to the built-in build agent. In
# particular, a config-defined agent defaults to mode="all" and may therefore
# run underneath Task; silently copying these tools into that agent would undo
# the structural no-media-in-subagents guarantee. An explicit config `tools`
# list remains the platform owner's opt-in escape hatch.
BUILD_ONLY_WORKFLOW_TOOLS = frozenset({
    "image_gen",
    "video_identity",
    "video_project",
    "video_generate",
    "video_transcribe",
    "video_render",
    "creator_context",
    "skill_manage",
})


# Built-in agent definitions
AGENTS: dict[str, AgentDef] = {
    "build": AgentDef(
        name="build",
        description="Default full-access agent for development tasks",
        tools=[
            "bash", "read", "write", "edit", "multiedit", "apply_patch", "glob", "grep",
            "task", "batch", "question", "todo_write", "todo_read",
            "plan_enter", "skill", "skill_search", "capability_search",
            "web_fetch", "web_search", "cron", "view_image",
            "share_file", "computer", "browser_mode",
            "image_gen", "video_identity", "video_project", "video_generate",
            "video_transcribe", "video_render", "creator_context", "skill_manage",
        ],
        max_steps=200,
        # prompt is None — dynamically selected based on model_id
        # via agent.prompts.system.get_system_prompt() in loop.py
        permission=[
            # Override defaults: build agent can ask questions and enter plan mode
            {"permission": "question", "pattern": "*", "action": "allow"},
            {"permission": "plan_enter", "pattern": "*", "action": "allow"},
        ],
    ),
    "plan": AgentDef(
        name="plan",
        description="Read-only agent for analysis and planning",
        tools=[
            "bash", "read", "write", "edit", "multiedit", "apply_patch", "glob", "grep",
            "task", "batch", "question", "plan_exit",
            "web_fetch", "web_search", "view_image", "browser_mode",
        ],
        permission=[
            # Override defaults: plan agent can ask questions and exit plan mode
            {"permission": "question", "pattern": "*", "action": "allow"},
            {"permission": "plan_exit", "pattern": "*", "action": "allow"},
            # Deny all edits (write/edit/apply_patch) by default
            {"permission": "edit", "pattern": "*", "action": "deny"},
            # Allow editing plan files only. Both spellings: the model is as
            # likely to write ".openbox/plans/x.md" from the workdir as the
            # absolute path, and the "**/" form does not match a path with no
            # leading segment — so the relative one used to be denied and plan
            # mode could not write its own plan.
            {"permission": "edit", "pattern": "**/.openbox/plans/*.md", "action": "allow"},
            {"permission": "edit", "pattern": ".openbox/plans/*.md", "action": "allow"},
            # Note: bash is NOT denied at the permission level (matching opencode).
            # Plan mode constraints are enforced via the system prompt, which tells
            # the agent to only use bash for read-only operations.
        ],
    ),
    "explore": AgentDef(
        name="explore",
        description='Fast agent specialized for exploring codebases. Use this when you need to quickly find files by patterns (eg. "src/components/**/*.tsx"), search code for keywords (eg. "API endpoints"), or answer questions about the codebase (eg. "how do API endpoints work?"). When calling this agent, specify the desired thoroughness level: "quick" for basic searches, "medium" for moderate exploration, or "very thorough" for comprehensive analysis across multiple locations and naming conventions.',
        tools=["bash", "read", "glob", "grep", "web_fetch", "web_search", "view_image"],
        max_steps=20,
        mode="subagent",
        subagent_capabilities=SUBAGENT_ALL_CAPABILITIES,
        prompt=PROMPT_EXPLORE,
        permission=[
            {"permission": "todoread", "pattern": "*", "action": "deny"},
            {"permission": "todowrite", "pattern": "*", "action": "deny"},
            {"permission": "task", "pattern": "*", "action": "deny"},
        ],
    ),
    "general": AgentDef(
        name="general",
        description="General-purpose agent for researching complex questions and executing multi-step tasks. Use this agent to execute multiple units of work in parallel.",
        tools=[
            "bash", "read", "write", "edit", "multiedit", "glob", "grep",
            "web_fetch", "web_search", "view_image", "computer", "browser_mode",
            # `skill` belongs beside `computer` and `browser_mode`: without it
            # this agent can open a browser and then has no way to drive one,
            # so it falls back to clicking pixels — the exact thing the system
            # prompt forbids. It grants no new authority either, since `bash`
            # can already run anything a skill would instruct; what it adds is
            # the instructions.
            "skill", "skill_search",
        ],
        max_steps=100,
        mode="subagent",
        subagent_capabilities=SUBAGENT_ALL_CAPABILITIES,
        prompt=PROMPT_GENERAL,
        permission=[
            {"permission": "todoread", "pattern": "*", "action": "deny"},
            {"permission": "todowrite", "pattern": "*", "action": "deny"},
            {"permission": "task", "pattern": "*", "action": "deny"},
        ],
    ),
    "compaction": AgentDef(
        name="compaction",
        description="Context compaction agent (summarizes conversation)",
        tools=[],
        hidden=True,
    ),
    "title": AgentDef(
        name="title",
        description="Title generation agent",
        tools=[],
        hidden=True,
        temperature=0.5,
        permission=[
            {"permission": "*", "pattern": "*", "action": "deny"},
        ],
        prompt=PROMPT_TITLE,
    ),
    "summary": AgentDef(
        name="summary",
        description="Summarises what a conversation actually changed",
        tools=[],
        hidden=True,
        permission=[
            {"permission": "*", "pattern": "*", "action": "deny"},
        ],
        prompt=PROMPT_SUMMARY,
    ),
}


#: Mode a config-defined agent gets when it does not say. opencode's default
#: too: someone adding an agent usually wants it both to chat with and to
#: spawn, and narrowing it later is easier than discovering it is missing.
DEFAULT_CONFIG_MODE = "all"

VALID_MODES = ("primary", "subagent", "all")

#: Named accents a UI can resolve, alongside a literal #rrggbb. Same set as
#: opencode's Agent.Color — an unrecognised value is dropped rather than
#: passed through, because it reaches the browser as an inline style.
NAMED_COLORS = ("primary", "secondary", "accent", "success", "warning", "error", "info")

_HEX_COLOR = _re.compile(r"#[0-9a-fA-F]{6}")


def valid_color(value: str | None) -> bool:
    """Whether a configured colour is one the UI can safely use."""
    if not value:
        return False
    return value in NAMED_COLORS or bool(_HEX_COLOR.fullmatch(value))


def _merged_registry() -> dict[str, AgentDef]:
    """The built-ins with the user's config folded in.

    Config can retune a built-in, hide it, remove it, or introduce an agent
    of its own — the same entry does all four, as in opencode. Resolved on
    every call rather than cached: config is reloaded at runtime, and a stale
    registry would silently ignore an edit the user just made.
    """
    import copy

    try:
        from core.config import get_config
        overrides = get_config().agent or {}
    except Exception:  # config not loaded (tests, tooling) — built-ins only
        return dict(AGENTS)

    registry = {name: copy.copy(a) for name, a in AGENTS.items()}
    for name, ov in overrides.items():
        if getattr(ov, "disable", False):
            registry.pop(name, None)
            continue
        agent = registry.get(name)
        if agent is None:
            agent = AgentDef(
                name=name,
                description=ov.description or f"{name} agent",
                tools=[
                    tool
                    for tool in AGENTS["build"].tools
                    if tool not in BUILD_ONLY_WORKFLOW_TOOLS
                ],
                mode=DEFAULT_CONFIG_MODE,
                portable_opt_in=(
                    "capability_search" in (getattr(ov, "tools", None) or ())
                ),
                subagent_capabilities=frozenset(
                    getattr(ov, "subagent_capabilities", None)
                    or SUBAGENT_BASE_CAPABILITIES
                ),
            )
        agent = apply_agent_overrides(copy.copy(agent), ov)
        registry[name] = agent
    return registry


def apply_agent_overrides(agent_def: AgentDef, overrides) -> AgentDef:
    """Layer per-agent config onto a definition, in place.

    `permission` accumulates rather than replaces: config rules are meant to
    tighten an agent's defaults, not discard them. `tools` does replace —
    a toolset is a whitelist, so accumulating would quietly widen an agent
    the user just narrowed.
    """
    if not overrides:
        return agent_def
    if overrides.model:
        agent_def.model = overrides.model
    if overrides.temperature is not None:
        agent_def.temperature = overrides.temperature
    if getattr(overrides, "max_steps", None) is not None:
        agent_def.max_steps = overrides.max_steps
    if overrides.prompt is not None:
        agent_def.prompt = overrides.prompt
    if overrides.permission:
        agent_def.permission = agent_def.permission + overrides.permission
    if getattr(overrides, "description", None):
        agent_def.description = overrides.description
    if getattr(overrides, "mode", None) in VALID_MODES:
        agent_def.mode = overrides.mode
    if getattr(overrides, "hidden", None) is not None:
        agent_def.hidden = overrides.hidden
    if getattr(overrides, "tools", None) is not None:
        agent_def.tools = _with_skill_search_companion(list(overrides.tools))
        # A non-build Agent only leaves the shadow rollout when its own
        # config whitelist explicitly includes the logical discovery slot.
        # Updating built-ins must follow the same rule as creating a custom
        # Agent; inherited/default tools are deliberately not consent.
        agent_def.portable_opt_in = "capability_search" in overrides.tools
    if valid_color(getattr(overrides, "color", None)):
        agent_def.color = overrides.color
    if getattr(overrides, "subagent_capabilities", None) is not None:
        requested = frozenset(overrides.subagent_capabilities)
        unknown = requested - SUBAGENT_ALL_CAPABILITIES
        if unknown:
            raise ValueError(
                "unknown subagent capability: " + ", ".join(sorted(unknown))
            )
        if not SUBAGENT_BASE_CAPABILITIES.issubset(requested):
            raise ValueError(
                "subagent capabilities must include model, agent_preset, and tool_filter"
            )
        agent_def.subagent_capabilities = requested
    return agent_def


def get_agent(name: str) -> AgentDef:
    """Get an agent definition by name."""
    # ``load_subagent_authority`` binds a private descriptor snapshot before a
    # child Loop resolves its AgentDef. Context-local lookup preserves the
    # exact accepted preset across hot config reload and cold worker resume.
    try:
        from agent.subagent_authority import current_frozen_subagent_agent

        frozen = current_frozen_subagent_agent(name)
        if frozen is not None:
            return frozen
    except ImportError:
        pass
    agent = _merged_registry().get(name)
    if not agent:
        raise ValueError(f"Unknown agent: {name}")
    return agent


def list_agents() -> list[AgentDef]:
    """The agents a user may pick to talk to.

    A subagent is not one of them. `explore` and `general` exist to be spawned
    by the task tool with a single self-contained prompt; they have no
    conversational prompt, and `explore` cannot even edit a file. Offering
    them as modes to chat in advertises two dead ends. (opencode draws the
    same line — `mode !== "subagent" && hidden !== true` — everywhere it
    lists agents for a person to choose from.)
    """
    return [a for a in _merged_registry().values() if a.mode != "subagent" and not a.hidden]


def list_subagents() -> list[AgentDef]:
    """The agents the task tool may spawn.

    The mirror of {@link list_agents}: anything not exclusively primary.
    Hidden agents are included — compaction and title are spawned by name,
    never chosen — matching opencode's `item.mode !== "primary"`.
    """
    return [a for a in _merged_registry().values() if a.mode != "primary"]


def default_agent_name() -> str:
    """The agent a new conversation starts in.

    Config may name one, but not any one: a subagent or a hidden agent
    cannot hold a conversation, and honouring such a value would strand
    every new chat. opencode raises in that case; we log and fall back,
    because this is read on the hot path for every new session and a
    deployment with one bad line in its config should still start chats.
    """
    from core.config import get_config

    try:
        wanted = get_config().default_agent
    except Exception:
        wanted = None
    pickable = {a.name for a in list_agents()}
    if wanted:
        if wanted in pickable:
            return wanted
        log.warning(f"default_agent '{wanted}' is not an agent a user can pick; using build")
    if "build" in pickable:
        return "build"
    return next(iter(sorted(pickable)), "build")


def is_subagent(name: str) -> bool:
    """Whether this agent may only run underneath another one."""
    agent = _merged_registry().get(name)
    return agent is not None and agent.mode == "subagent"
