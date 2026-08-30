"""Skill tool: load and inject skill content."""
from __future__ import annotations

import html
import re
from dataclasses import replace

from pydantic import BaseModel, Field

from core.log import create_logger
from tool.tool import ToolResult, ToolContext, ToolInfo, define_tool

log = create_logger("tool.skill")


class SkillArgs(BaseModel):
    skill: str = Field(description="Name of the skill to load")
    args: str = Field(default="", description="Optional arguments for the skill")


class SkillSearchArgs(BaseModel):
    query: str = Field(
        default="",
        max_length=500,
        description="Short words describing the needed skill.",
    )
    name: str | None = Field(
        default=None,
        max_length=500,
        description="Optional exact skill name. Exact lookup does not fall back to fuzzy matching.",
    )


#: Where the container keeps user-installed skills, and the convenience symlink
#: inside the workspace that points at it. Built-in skills live elsewhere and
#: are not covered by that link.
USER_SKILLS_DIR = "/data/skills"
WORKSPACE_SKILLS_LINK = "/workspace/skills"

_BASE_DESCRIPTION = """\
Load a specialized skill that provides domain-specific instructions and workflows.

When you recognize that a task matches one of the available skills, use this tool to load the full skill instructions. The skill will inject detailed instructions, workflows, and access to bundled resources into the conversation context.

Usage:
- Use the skill name as the parameter (e.g., "dev-browser")
- Skills can accept optional arguments via the args parameter
- When a user references a "slash command" or "/<something>", it may map to a skill — use this tool to load it
- The skill content will be returned in the tool output for you to follow"""


# Never worth naming: the model cannot act on them and they crowd out the files
# that matter. A single skill with a node_modules tree contributed 890 entries,
# which is what a 50-line listing gets spent on if nothing filters it.
_SKIP_DIRS = {"node_modules", ".git", "__pycache__", ".venv", "venv",
              "dist", "build", ".next", ".cache"}


def _host_files(base: str, limit: int = 20) -> list[str]:
    """Files bundled beside a host skill's SKILL.md, sampled.

    SKILL.md itself is excluded — its content is inlined right above the
    listing. So are dotfiles and macOS AppleDouble stubs (._foo), which appear
    whenever a skill has been zipped on a Mac.
    """
    from pathlib import Path
    try:
        out = []
        root = Path(base)
        for f in sorted(root.rglob("*")):
            if not f.is_file():
                continue
            rel = f.relative_to(root)
            if any(p in _SKIP_DIRS for p in rel.parts):
                continue
            if any(p.startswith(".") for p in rel.parts):
                continue
            if rel.name == "SKILL.md":
                continue
            out.append(str(rel))
            if len(out) >= limit:
                break
        return out
    except OSError:
        return []


async def execute(args: SkillArgs, ctx: ToolContext) -> ToolResult:
    """Load a skill and inject its content.

    A project skill on the backend host is the checked-out application's
    authoritative workflow and therefore overrides a stale copy baked into a
    remote sandbox. User/global skills keep the original container-first
    behaviour because the sandbox copy is the one whose bundled files the
    agent can reach.
    """
    content = None
    base_dir = ""
    files: list[str] = []
    host_only = False
    container_error: str | None = None

    from skill.skill import get_skill
    local_skill = await get_skill(args.skill)

    # The project checkout is what local development and browser testing are
    # validating. Letting an older /opt/openbox copy win made edits appear to
    # have no effect until somebody redeployed the cloud desktop.
    if local_skill and local_skill.source == "project":
        content = local_skill.content
        host_only = True
        files = _host_files(local_skill.path) if local_skill.path else []
        log.info(
            f"Using authoritative {local_skill.source} host copy of skill {args.skill!r}; it overrides any "
            "sandbox copy"
        )

    # Try loading from the container when no project-local override exists.
    if not content and ctx.sandbox:
        try:
            skill_data = await ctx.sandbox.get_skill(args.skill)
            content = skill_data.get("content", "")
            base_dir = skill_data.get("base_dir", "")
            files = skill_data.get("files", [])
            # Invariant: no field in a skill file, from any source, may change
            # the agent's callable tool set. Container skills are untrusted
            # user data, so keep a diagnostic for obsolete declarations but
            # discard them at this parsing boundary.
            from core.markdown import parse_frontmatter
            metadata, _ = parse_frontmatter(content)
            declaration_keys = {
                key
                for key in ("allowed-tools", "allowed_tools", "tools")
                if key in skill_data or key in metadata
            }
            if declaration_keys:
                log.debug(
                    f"Ignoring documentary tool fields from untrusted container skill "
                    f"{args.skill!r}: {', '.join(sorted(declaration_keys))}"
                )
        except Exception as e:
            # Kept, not swallowed: an unreachable container and a genuinely
            # missing skill need different answers. Reporting "not found" for a
            # dropped tunnel tells the model to give up on a skill that exists.
            import httpx

            if isinstance(e, httpx.HTTPStatusError) and e.response.status_code == 404:
                # The real SandboxClient raises for a missing /skills/{name}.
                # A 404 is a normal catalogue miss, not evidence that the
                # WUYING tunnel or action server is unavailable.
                log.debug(f"Container has no skill named {args.skill!r}")
            else:
                container_error = str(e) or e.__class__.__name__
                log.debug(f"Container lookup for skill {args.skill!r} failed: {e}")

    # Fall back to local skills
    if not content:
        skill = local_skill
        if not skill:
            if container_error:
                return ToolResult(
                    title=f"Could not reach the container to load: {args.skill}",
                    output=(
                        f"The skill could not be loaded because the container was "
                        f"unreachable: {container_error}\n"
                        f"No skill named '{args.skill}' exists on the backend host "
                        f"either, so this may still be a valid skill. This is an "
                        f"infrastructure failure, not a missing skill — retrying "
                        f"may work."
                    ),
                    metadata={"error": "container_unreachable"},
                )
            return ToolResult(
                title=f"Skill not found: {args.skill}",
                output=f"No skill named '{args.skill}' found.",
            )
        content = skill.content
        # A host skill's bundled files live on the backend, which the agent's
        # tools cannot see — they run in the sandbox. Name them, but do not hand
        # over a path the model would only fail to read.
        host_only = True
        files = _host_files(skill.path) if skill.path else []

    if args.args:
        content = content.replace("$ARGUMENTS", args.args)

    # Build output following opencode's pattern: content + base dir + file listing
    output_parts = [
        f"<skill_content name=\"{args.skill}\">",
        content.strip(),
    ]
    if base_dir:
        output_parts.append("")
        output_parts.append(f"Base directory for this skill: {base_dir}")
        output_parts.append("Relative paths in this skill should be resolved from this directory.")
        # Only user-installed skills live under the path /workspace/skills points
        # at. Saying this for a built-in skill sent the model to an empty
        # directory, one line after being given the correct base directory.
        if base_dir.startswith(USER_SKILLS_DIR):
            output_parts.append(
                f"The skill directory is also reachable at {WORKSPACE_SKILLS_LINK}/.")
    if files:
        output_parts.append("")
        if host_only:
            output_parts.append("This skill is installed on the backend host, not in")
            output_parts.append("your workspace. The files below ship with it but are")
            output_parts.append("NOT readable from here — follow the instructions above")
            output_parts.append("without trying to open them.")
        output_parts.append("<skill_files>")
        for f in files[:50]:
            output_parts.append(f"  {f}")
        if len(files) > 50:
            output_parts.append(f"  ... and {len(files) - 50} more files")
        output_parts.append("</skill_files>")
    output_parts.append("</skill_content>")

    if args.skill == "dev-browser":
        output_parts.append("")
        output_parts.append(await _browser_readiness(ctx))

    return ToolResult(
        title=f"Loaded skill: {args.skill}",
        output="\n".join(output_parts),
    )


async def _browser_readiness(ctx: ToolContext) -> str:
    """Bring up the browser this user chose, and say which one they got.

    Done at skill-load time so the model never writes a script against a
    browser that is not there. The fallback matters most: when someone has
    asked for their own browser but the extension is not connected, the run
    continues on the cloud desktop's Chrome rather than failing — the model
    just has to know, because the two have different logins.
    """
    from session.browser_pref import get_browser_mode, relay_mode

    preference = await get_browser_mode(ctx.user_id)
    if not ctx.sandbox:
        return f"<browser_mode>preference={preference}; no sandbox, browser unavailable</browser_mode>"

    try:
        from sandbox.browser import ensure_browser
        state = await ensure_browser(ctx.sandbox, ctx.session_id, relay_mode(preference))
    except Exception as e:
        log.warning(f"browser readiness failed: {e}")
        return (
            f"<browser_mode>preference={preference}; the browser could not be started: "
            f"{str(e)[:200]}. Report this rather than retrying blindly.</browser_mode>"
        )

    effective = state.get("mode", "unknown")
    lines = [
        "<browser_mode>",
        f"  preference: {preference}",
        f"  running as: {effective}",
    ]
    if effective == "local":
        lines.append("  This is the cloud desktop's Chrome — it does NOT have the user's logins.")
        lines.append(
            "  It runs on this desktop, so if a native dialog blocks a script you can "
            "dismiss it with the `computer` tool and resume (see the skill's handoff section)."
        )
        if preference == "remote":
            lines.append(
                "  The user asked for their own browser, but the extension is not connected, "
                "so this fell back. Tell them if the task needs their accounts."
            )
    elif effective == "extension":
        lines.append("  This is the user's OWN Chrome, with their real sessions.")
        lines.append(
            "  It runs on the user's machine, so `computer` cannot see or touch it. "
            "If a native dialog blocks a script, tell the user what to dismiss."
        )
    lines.append("</browser_mode>")
    return "\n".join(lines)


skill_tool = define_tool(
    "skill",
    description=_BASE_DESCRIPTION,
    parameters=SkillArgs,
    execute=execute,
    sandbox_required=False,
    never_prune=True,
)


# The listing rides along on every request of every step, so it is budgeted
# rather than unbounded. Measured on synthetic catalogues: at full description a
# 250-skill install rendered 16,454 tokens per request, and a single skill whose
# frontmatter description had run to 40k characters rendered 10,188 on its own.
#
# A frontmatter description is a one-line trigger hint by design — enough for the
# model to decide whether to load the skill, which is when the real content
# arrives. Anything past that is paid for on every request and read on none.
from core.markdown import MAX_DESCRIPTION_CHARS, clip_description as _clip

LISTING_BUDGET_TOKENS = 2_000
LISTING_HARD_CHARS_DEFAULT = 8_000
SKILL_SEARCH_MAX_RESULTS = 5
SKILL_SEARCH_RESULT_CHARS = 2_000
SKILL_SEARCH_INDEX_NAME_CHARS = 500

_SEARCH_WORDS = re.compile(r"[\w.-]+", re.UNICODE)
_XML_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SEARCH_NOTICE = (
    "  <notice>More skills are available; use skill_search to find them.</notice>"
)


class SkillListingCompanionRequired(RuntimeError):
    """A complete Skill directory cannot fit without its search companion."""


def _xml_text(value, limit: int | None = None) -> str:
    """Return XML-safe text, optionally bounded without splitting entities."""
    raw = _XML_CONTROL.sub("", str(value or ""))
    if limit is None:
        return html.escape(raw, quote=True)
    if limit <= 0:
        return ""

    escaped: list[str] = []
    used = 0
    for character in raw:
        fragment = html.escape(character, quote=True)
        if used + len(fragment) > limit:
            suffix = "…"
            while escaped and used + len(suffix) > limit:
                used -= len(escaped.pop())
            return "".join(escaped) + suffix[: max(0, limit - used)]
        escaped.append(fragment)
        used += len(fragment)
    return "".join(escaped)


def _listing_hard_chars() -> int:
    try:
        from core.config import get_config

        return int(get_config().tool_exposure.skill_listing_hard_chars)
    except Exception:
        return LISTING_HARD_CHARS_DEFAULT


def _listing_entries(skills: list[dict]) -> list[tuple[str, str]]:
    entries = []
    for skill in skills:
        name = str(skill.get("name") or "")
        if name:
            entries.append((name, _clip(skill.get("description", ""))))
    return sorted(entries, key=lambda item: item[0])


def _full_listing_block(name: str, description: str) -> list[str]:
    return [
        "  <skill>",
        f"    <name>{_xml_text(name)}</name>",
        f"    <description>{_xml_text(description)}</description>",
        "  </skill>",
    ]


def _permitted(skills: list[dict], ruleset: list) -> list[dict]:
    """Drop skills the agent is denied, so its catalogue matches its reach.

    Mirrors opencode's SkillV2.available(). Listing a skill the agent cannot
    load costs tokens on every request to advertise a guaranteed refusal.
    """
    if not ruleset:
        return skills
    from permission.permission import evaluate
    out = []
    for s in skills:
        try:
            if evaluate("skill", s.get("name", ""), ruleset).action != "deny":
                out.append(s)
        except Exception:
            out.append(s)  # a malformed rule must not hide a working skill
    return out


def render_listing(skills: list[dict], budget: int = LISTING_BUDGET_TOKENS) -> str:
    """Render the complete legacy listing used by the PR#0 meter.

    Names are kept for every skill even when descriptions have to go: the model
    can still load one by name. This function deliberately remains unbounded so
    measurement sees the complete would-be wire. PR#5 applies its hard cap only
    after this value has been measured and only when ``skill_search`` is present.
    """
    from core.token import token_estimate

    entries = _listing_entries(skills)
    lines = ["<available_skills>"]
    spent = 0
    names_only: list[str] = []

    for name, description in entries:
        if names_only:
            names_only.append(name)
            continue
        block = _full_listing_block(name, description)
        cost = token_estimate("\n".join(block))
        if spent + cost > budget:
            names_only.append(name)
            continue
        lines.extend(block)
        spent += cost

    if names_only:
        log.info(f"Skill listing over budget: {len(names_only)} of {len(entries)} "
                 f"listed by name only")
        lines.append("  <!-- Description budget reached. These skills are also")
        lines.append("       available; call the tool by name to see what one does. -->")
        for name in names_only:
            lines.append(f"  <skill><name>{_xml_text(name)}</name></skill>")

    lines.append("</available_skills>")
    rendered = "\n".join(lines)
    hard_chars = _listing_hard_chars()
    if len(rendered) > hard_chars:
        # PR#0 stays measurement-only. The caller performs the PR#5 bounded
        # materialization only after it has also built a searchable index.
        log.warning(
            "skill_listing over_hard_budget chars=%s hard=%s entries=%s; "
            "complete meter preserved",
            len(rendered),
            hard_chars,
            len(entries),
        )
    return rendered


def _render_bounded_listing(
    skills: list[dict],
    *,
    hard_chars: int,
    budget: int = LISTING_BUDGET_TOKENS,
) -> str:
    """Render a stable XML prefix plus a fixed search notice under the cap."""
    from core.token import token_estimate

    entries = _listing_entries(skills)
    lines = ["<available_skills>"]
    spent = 0
    descriptions_exhausted = False
    closing = "</available_skills>"

    for name, description in entries:
        full = _full_listing_block(name, description)
        cost = token_estimate("\n".join(full))
        if descriptions_exhausted or spent + cost > budget:
            descriptions_exhausted = True
            block = [f"  <skill><name>{_xml_text(name)}</name></skill>"]
        else:
            block = full

        candidate = "\n".join([*lines, *block, _SEARCH_NOTICE, closing])
        if len(candidate) > hard_chars:
            break
        lines.extend(block)
        if not descriptions_exhausted:
            spent += cost

    rendered = "\n".join([*lines, _SEARCH_NOTICE, closing])
    # Config validation keeps this at >=500. Stay fail-closed if a test or
    # hand-built config violates that contract: never emit an oversized wire.
    if len(rendered) > hard_chars:
        rendered = "\n".join([
            "<available_skills>",
            "  <notice>Use skill_search to find available skills.</notice>",
            closing,
        ])
    if len(rendered) > hard_chars:
        raise ValueError("skill listing hard cap is too small for the search notice")
    return rendered


def _skill_search_index(skills: list[dict]) -> tuple[tuple[str, str], ...]:
    """Detach only permitted names and short hints; never retain Skill bodies."""
    index: dict[str, str] = {}
    for skill in skills:
        name = str(skill.get("name") or "")
        # Valid installed names are short slugs. Refuse an adversarial host
        # frontmatter name that would otherwise make every lexical scan
        # unbounded; the tool schema enforces the same exact-name ceiling.
        if not name or len(name) > SKILL_SEARCH_INDEX_NAME_CHARS or name in index:
            continue
        index[name] = _clip(skill.get("description", ""))
    return tuple(sorted(index.items(), key=lambda item: item[0]))


def _rank_skill_search(
    index: tuple[tuple[str, str], ...],
    args: SkillSearchArgs,
) -> list[tuple[str, str]]:
    if args.name is not None:
        wanted = args.name.strip()
        return [item for item in index if item[0] == wanted][:1]

    query = args.query.strip().casefold()
    terms = [word.casefold() for word in _SEARCH_WORDS.findall(query)[:16] if word]
    if not terms:
        return []

    ranked: list[tuple[int, str, str]] = []
    for name, hint in index:
        folded_name = name.casefold()
        folded_hint = hint.casefold()
        score = 0
        if query == folded_name:
            score += 100
        elif folded_name.startswith(query):
            score += 40
        elif query in folded_name:
            score += 20
        for term in terms:
            if term == folded_name:
                score += 20
            elif folded_name.startswith(term):
                score += 8
            elif term in folded_name:
                score += 4
            elif term in folded_hint:
                score += 1
        if score:
            ranked.append((score, name, hint))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return [(name, hint) for _score, name, hint in ranked[:SKILL_SEARCH_MAX_RESULTS]]


def _render_skill_search_results(
    matches: list[tuple[str, str]],
    *,
    hard_chars: int = SKILL_SEARCH_RESULT_CHARS,
) -> tuple[str, int]:
    if not matches:
        message = "No permitted skills matched. Try an exact name or narrower keywords."
        return (
            message[:hard_chars],
            0,
        )

    closing = "</skill_search_results>"
    lines = ["<skill_search_results>"]
    rendered_count = 0
    for name, hint in matches:
        block = [
            "  <skill>",
            f"    <name>{_xml_text(name, 120)}</name>",
            f"    <hint>{_xml_text(hint, 140)}</hint>",
            "  </skill>",
        ]
        candidate = "\n".join([*lines, *block, closing])
        if len(candidate) > hard_chars:
            break
        lines.extend(block)
        rendered_count += 1
    rendered = "\n".join([*lines, closing])
    if len(rendered) > hard_chars:
        raise AssertionError("skill search renderer exceeded its hard cap")
    return rendered, rendered_count


async def _execute_skill_search(
    args: SkillSearchArgs,
    ctx: ToolContext,
    index: tuple[tuple[str, str], ...],
) -> ToolResult:
    max_calls = max(1, int(ctx._capability_max_search_calls))
    max_reveals = max(1, int(ctx._capability_max_reveals))
    max_result_chars = max(100, int(ctx._capability_max_result_chars))
    if ctx._capability_search_calls >= max_calls:
        return ToolResult(
            title="Skill search limit reached",
            output="This step already used the bounded capability-search limit.",
            metadata={"blocked": True},
        )
    ctx._capability_search_calls += 1

    remaining_ids = max_reveals - len(ctx._capability_revealed_ids)
    remaining_chars = max_result_chars - ctx._capability_result_chars
    if remaining_ids <= 0 or remaining_chars <= 0:
        return ToolResult(
            title="Skill result limit reached",
            output="The bounded capability-result budget for this step is exhausted.",
            metadata={"blocked": True},
        )
    minimum_envelope_chars = len(
        "<skill_search_results>\n</skill_search_results>"
    )
    if remaining_chars < minimum_envelope_chars:
        return ToolResult(
            title="Skill result limit reached",
            output="The bounded capability-result budget for this step is exhausted.",
            metadata={"blocked": True},
        )

    matches = _rank_skill_search(index, args)
    unseen = [
        item
        for item in matches
        if f"skill:{item[0]}" not in ctx._capability_revealed_ids
    ][:remaining_ids]
    output, rendered_count = _render_skill_search_results(
        unseen,
        hard_chars=min(SKILL_SEARCH_RESULT_CHARS, remaining_chars),
    )
    selected = unseen[:rendered_count]
    if matches and not selected:
        return ToolResult(
            title="Skill result budget exhausted",
            output="Matching skills exceeded the remaining bounded result budget.",
            metadata={"blocked": True},
        )
    ctx._capability_revealed_ids.update(
        f"skill:{name}" for name, _hint in selected
    )
    ctx._capability_result_chars += len(output)
    return ToolResult(
        title=f"Found {len(selected)} skills" if selected else "No matching skills",
        output=output,
        metadata={"count": len(selected)},
    )


def _skill_search_for(skills: list[dict]) -> ToolInfo:
    index = _skill_search_index(skills)

    async def search(args: SkillSearchArgs, ctx: ToolContext) -> ToolResult:
        return await _execute_skill_search(args, ctx, index)

    return define_tool(
        "skill_search",
        description=(
            "Search the permitted Skill directory by exact name or short keywords. "
            "Returns at most five names and short hints; use skill(name=...) next to "
            "load the selected instructions."
        ),
        parameters=SkillSearchArgs,
        execute=search,
        sandbox_required=False,
        parallel_safe=False,
        discovery_hint="Find a permitted Skill whose name is not in the bounded listing.",
    )


async def _empty_skill_search(args: SkillSearchArgs, ctx: ToolContext) -> ToolResult:
    return await _execute_skill_search(args, ctx, ())


skill_search_tool = define_tool(
    "skill_search",
    description=(
        "Search the permitted Skill directory by exact name or short keywords. "
        "This companion is exposed only when the Skill listing exceeds its hard cap."
    ),
    parameters=SkillSearchArgs,
    execute=_empty_skill_search,
    sandbox_required=False,
    parallel_safe=False,
    discovery_hint="Find a permitted Skill whose name is not in the bounded listing.",
)


async def _collect_permitted_skills(
    sandbox=None,
    ruleset: list | None = None,
) -> list[dict]:
    """Merge container/global/project directories, then authorize every row."""
    skills: list[dict] = []

    if sandbox:
        try:
            container_skills = await sandbox.list_skills()
            if isinstance(container_skills, list):
                skills.extend(container_skills)
        except Exception:
            pass

    try:
        from skill.skill import list_skills as list_local_skills
        local = await list_local_skills()
        container_positions = {
            cs.get("name"): index for index, cs in enumerate(skills) if cs.get("name")
        }
        for s in local:
            entry = {
                "name": s.name,
                "description": s.description,
                "source": s.source,
            }
            position = container_positions.get(s.name)
            if position is not None:
                if s.source == "project":
                    skills[position] = entry
                    log.info(
                        f"Skill {s.name!r} exists in both the sandbox and project; "
                        "using the project host copy"
                    )
                else:
                    log.info(
                        f"Skill {s.name!r} exists in both the sandbox and on the "
                        "host; using the sandbox copy"
                    )
                continue
            container_positions[s.name] = len(skills)
            skills.append(entry)
    except Exception:
        pass

    return _permitted(skills, ruleset or [])


async def build_skill_tools_with_listing(
    sandbox=None,
    ruleset: list | None = None,
    *,
    enable_search: bool = True,
) -> tuple[ToolInfo, ToolInfo | None]:
    """Build the Skill loader and its conditional, same-step search companion."""
    skills = await _collect_permitted_skills(sandbox, ruleset)

    if not skills:
        return skill_tool, None

    complete_listing = render_listing(skills)
    hard_chars = _listing_hard_chars()
    search: ToolInfo | None = None
    wire_listing = complete_listing
    if len(complete_listing) > hard_chars:
        if enable_search:
            # Build the permission-filtered index before dropping any wire row.
            # Truncation and discovery therefore become one atomic materialization.
            search = _skill_search_for(skills)
            wire_listing = _render_bounded_listing(skills, hard_chars=hard_chars)
        else:
            raise SkillListingCompanionRequired(
                "Skill directory exceeds its hard cap without an eligible "
                "skill_search companion"
            )

    enriched_description = "\n".join([_BASE_DESCRIPTION, "", wire_listing])
    return replace(skill_tool, description=enriched_description), search


async def build_skill_tool_with_listing(
    sandbox=None,
    ruleset: list | None = None,
) -> ToolInfo:
    """Compatibility helper that never truncates without returning a search tool."""
    tool, _search = await build_skill_tools_with_listing(
        sandbox,
        ruleset,
        enable_search=False,
    )
    return tool
