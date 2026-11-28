"""A4-A7: Skill catalogue, reload dedupe, stale-copy pruning and the @skill rule.

Each change sits behind an agent_tuning switch. Off must mean today's request
bytes and today's pruning; on is tested for what docs/KERNEL_DSH_COMPARISON_
20261128.md section 21 asks of it.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

import core.config as config_mod
import skill.skill as sk
from agent import compaction
from agent.agent import AgentDef
from agent.processor import persisted_tool_metadata
from agent.tool_resolution import resolve_step_tools
from core.config import AgentTuningConfig, OpenBoxConfig
from permission.permission import Rule
from tool import skill_tool as st
from tool.tool import ToolContext, ToolInfo


@pytest.fixture(autouse=True)
def registered_tools():
    from tool.registry import register_builtin_tools

    register_builtin_tools(load_custom=False)


@pytest.fixture
def tuning(monkeypatch):
    def install(**fields):
        cfg = OpenBoxConfig(agent_tuning=AgentTuningConfig(**fields))
        monkeypatch.setattr(config_mod, "_config", cfg)
        return cfg

    install()
    return install


def _reset_skill_cache(monkeypatch):
    monkeypatch.setattr(sk, "_skills", {})
    monkeypatch.setattr(sk, "_loaded", False)
    monkeypatch.setattr(sk, "_fingerprint", ())
    monkeypatch.setattr(sk, "_last_check", 0.0)
    monkeypatch.setattr(sk, "_CHECK_INTERVAL_SECONDS", 0.0)


@pytest.fixture
def project(monkeypatch):
    """A throwaway backend checkout with two built-in Skills."""
    root = Path(tempfile.mkdtemp()).resolve()
    base = root / ".openbox" / "skills"
    for name, description in (("alpha", "First workflow. " * 30), ("beta", "Second <b>&</b> workflow")):
        (base / name).mkdir(parents=True)
        (base / name / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {description}\n---\nDo {name} things.\n"
        )
    cwd = os.getcwd()
    os.chdir(root)
    _reset_skill_cache(monkeypatch)
    yield base
    os.chdir(cwd)
    shutil.rmtree(root, ignore_errors=True)


AGENT = AgentDef(name="build", description="test",
                 tools=["capability_search", "skill", "skill_search", "bash"])


async def _deferred(rules=(), workspace_id=None, monkeypatch=None):
    if monkeypatch is not None:
        from agent import tool_resolution as mod

        async def forbidden(*args, **kwargs):
            raise AssertionError("an ordinary step must not read the sandbox directory")

        monkeypatch.setattr(mod, "_catalogue_view", forbidden)
        monkeypatch.setattr(mod, "attach_skill_listing", forbidden)
    # object() has no sandbox methods: any tunnel read would raise.
    return await resolve_step_tools(
        AGENT, object(), list(rules), user_id="owner", project_id="project",
        workdir="/workspace/project", defer_remote=True, return_catalogue_state=True,
        compact_skills=True, workspace_id=workspace_id,
    )


# ── A4: resident built-in catalogue ─────────────────────────────────────────

async def test_switches_off_keep_the_deferred_description_byte_identical(tuning, project, monkeypatch):
    result = await _deferred(monkeypatch=monkeypatch)
    assert result.deferred
    assert result.tools["skill"].description == st.COMPACT_SKILL_DESCRIPTION


async def test_catalogue_is_resident_without_any_directory_io(tuning, project, monkeypatch):
    tuning(resident_skill_catalogue=True)
    result = await _deferred(monkeypatch=monkeypatch)
    description = result.tools["skill"].description
    assert description.startswith(st.CATALOGUE_SKILL_DESCRIPTION)
    assert "  - alpha: First workflow." in description
    # Element text is escaped; the hint is one clipped line.
    assert "  - beta: Second &lt;b&gt;&amp;&lt;/b&gt; workflow" in description
    alpha_line = next(line for line in description.splitlines() if line.startswith("  - alpha"))
    assert len(alpha_line) < len("  - alpha: ") + st.RESIDENT_DESCRIPTION_CHARS + 2
    assert "skill_search" in description


async def test_catalogue_obeys_skill_permissions(tuning, project, monkeypatch):
    tuning(resident_skill_catalogue=True)
    deny = Rule(permission="skill", pattern="alpha", action="deny")
    description = (await _deferred([deny], monkeypatch=monkeypatch)).tools["skill"].description
    assert "alpha" not in description and "beta" in description


async def test_workspace_list_narrows_the_catalogue(tuning, project, monkeypatch):
    tuning(resident_skill_catalogue=True, workspaces=["ws_internal"])
    other = await _deferred(workspace_id="ws_customer", monkeypatch=monkeypatch)
    assert other.tools["skill"].description == st.COMPACT_SKILL_DESCRIPTION
    internal = await _deferred(workspace_id="ws_internal", monkeypatch=monkeypatch)
    assert "  - alpha" in internal.tools["skill"].description


async def test_resolved_portable_step_lists_the_same_built_ins_and_no_sandbox_rows(tuning, project, monkeypatch):
    class Sandbox:
        async def list_skills(self):
            return [{"name": "sandbox-only", "description": "Untrusted <text>"}]

    off, _ = await st.build_skill_tools_with_listing(Sandbox(), [], enable_search=True, compact=True)
    assert off.description == st.COMPACT_SKILL_DESCRIPTION

    tuning(resident_skill_catalogue=True, skill_mention_rule=True)
    deferred = (await _deferred()).tools["skill"].description

    loader, search = await st.build_skill_tools_with_listing(
        Sandbox(), [], enable_search=True, compact=True,
    )
    assert loader.description == deferred
    assert "sandbox-only" not in loader.description
    assert search is not None  # the sandbox row stays discoverable through search


def test_catalogue_stays_inside_the_listing_budget():
    skills = [{"name": f"skill-{index:04d}", "description": "x" * 500} for index in range(400)]
    rendered = st._render_resident_catalogue(skills, hard_chars=st.LISTING_HARD_CHARS_DEFAULT)
    assert 0 < len(rendered) <= st.LISTING_HARD_CHARS_DEFAULT
    assert rendered.endswith("</available_skills>") and "skill_search" in rendered
    from core.token import token_estimate
    described = [line for line in rendered.splitlines() if ": " in line and line.startswith("  - ")]
    assert token_estimate("\n".join(described)) <= st.LISTING_BUDGET_TOKENS


async def test_the_shipped_built_in_catalogue_fits_the_budget(tuning, monkeypatch):
    _reset_skill_cache(monkeypatch)
    backend = Path(st.__file__).resolve().parents[1]
    monkeypatch.chdir(backend)
    catalogue = await st.resident_skill_catalogue([])
    from core.token import token_estimate
    assert "video-production" in catalogue and "footage-edit" in catalogue
    assert len(catalogue) <= st.LISTING_HARD_CHARS_DEFAULT
    assert token_estimate(catalogue) <= st.LISTING_BUDGET_TOKENS


# ── A7 stage one: the @skill:<name> rule ────────────────────────────────────

async def test_mention_rule_reaches_every_loader_description(tuning, project, monkeypatch):
    assert st.SKILL_MENTION_RULE not in st.COMPACT_SKILL_DESCRIPTION
    tuning(skill_mention_rule=True)
    deferred = (await _deferred(monkeypatch=monkeypatch)).tools["skill"].description
    assert deferred == f"{st.COMPACT_SKILL_DESCRIPTION} {st.SKILL_MENTION_RULE}"
    assert "@skill:<name>" in deferred

    full, _ = await st.build_skill_tools_with_listing(None, [], enable_search=False)
    assert f"- {st.SKILL_MENTION_RULE}" in full.description

    tuning()
    full_off, _ = await st.build_skill_tools_with_listing(None, [], enable_search=False)
    assert full_off.description.startswith(st._BASE_DESCRIPTION + "\n\n<available_skills>")
    assert st.SKILL_MENTION_RULE not in full_off.description


# ── A5: unchanged reloads ───────────────────────────────────────────────────

def _skill_part(name, *, digest=None, unchanged=False, compacted=None, output="body", part_id="p"):
    metadata = {}
    if digest:
        metadata["skill_sha256"] = digest
    if unchanged:
        metadata["skill_unchanged"] = True
    return {"type": "tool", "id": part_id, "tool": "skill", "status": "completed",
            "input": {"skill": name}, "output": output, "metadata": metadata,
            "title": f"Loaded skill: {name}", "state": {"time": {"compacted": compacted}}}


def _view(*parts):
    return (SimpleNamespace(id="m1", role="assistant", parts=list(parts)),)


async def _load(name="alpha", view=None, workspace_id="ws"):
    ctx = ToolContext(session_id="s", workspace_id=workspace_id)
    ctx._history_view = view
    return await st.execute(st.SkillArgs(skill=name), ctx)


async def test_digest_is_always_reported_and_off_never_drops_the_body(tuning, project):
    first = await _load()
    digest = first.metadata["skill_sha256"]
    assert len(digest) == 64 and "Do alpha things." in first.output
    again = await _load(view=_view(_skill_part("alpha", digest=digest)))
    assert again.output == first.output and "skill_unchanged" not in again.metadata


async def test_unchanged_body_still_in_view_is_not_sent_twice(tuning, project):
    tuning(skill_reload_dedupe=True)
    first = await _load()
    digest = first.metadata["skill_sha256"]
    again = await _load(view=_view(_skill_part("alpha", digest=digest)))
    assert "Do alpha things." not in again.output
    assert "<skill_unchanged" in again.output and "load the skill again" in again.output
    assert again.metadata == {"skill_sha256": digest, "skill_unchanged": True}
    # The Light gate recognises a load by this prefix.
    assert again.title.startswith("Loaded skill")


@pytest.mark.parametrize("earlier", [
    "changed", "pruned", "no_digest", "other_skill", "only_a_pointer", "older_copy_matches",
])
async def test_anything_but_a_visible_identical_body_returns_the_full_text(tuning, project, earlier):
    tuning(skill_reload_dedupe=True)
    digest = (await _load()).metadata["skill_sha256"]
    parts = {
        "changed": [_skill_part("alpha", digest="0" * 64)],
        "pruned": [_skill_part("alpha", digest=digest, compacted=1.0)],
        "no_digest": [_skill_part("alpha")],
        "other_skill": [_skill_part("beta", digest=digest)],
        "only_a_pointer": [_skill_part("alpha", digest=digest, unchanged=True)],
        # The newest copy decides: an app update since then must win.
        "older_copy_matches": [_skill_part("alpha", digest=digest, part_id="old"),
                               _skill_part("alpha", digest="1" * 64, part_id="new")],
    }[earlier]
    result = await _load(view=_view(*parts))
    assert "Do alpha things." in result.output
    assert "skill_unchanged" not in result.metadata


async def test_an_edited_skill_delivers_its_new_body(tuning, project):
    tuning(skill_reload_dedupe=True)
    first = await _load()
    (project / "alpha" / "SKILL.md").write_text("---\nname: alpha\ndescription: d\n---\nNew alpha steps.\n")
    second = await _load(view=_view(_skill_part("alpha", digest=first.metadata["skill_sha256"])))
    assert "New alpha steps." in second.output
    assert second.metadata["skill_sha256"] != first.metadata["skill_sha256"]


async def test_dedupe_follows_the_workspace_list(tuning, project):
    tuning(skill_reload_dedupe=True, workspaces=["ws_internal"])
    digest = (await _load()).metadata["skill_sha256"]
    view = _view(_skill_part("alpha", digest=digest))
    assert "Do alpha things." in (await _load(view=view, workspace_id="ws_customer")).output
    assert "<skill_unchanged" in (await _load(view=view, workspace_id="ws_internal")).output


def test_digest_and_pointer_marker_survive_persistence():
    kept = persisted_tool_metadata({"skill_sha256": "a" * 64, "skill_unchanged": True,
                                    "provider": "host-builtin"})
    assert kept == {"skill_sha256": "a" * 64, "skill_unchanged": True}


# ── A6 and never_prune ─────────────────────────────────────────────────────

class Msg:
    def __init__(self, id, role, parts=None, summary=False):
        self.id = id
        self.role = role
        self.parts = parts or []
        self.summary = summary


def _tool(part_id, tool, output_tokens, **extra):
    part = {"type": "tool", "id": part_id, "message_id": f"m-{part_id}", "tool": tool,
            "status": "completed", "output": "x" * (output_tokens * 4), "state": {"time": {}}}
    part.update(extra)
    return part


def _skill(part_id, name, tokens=12_000, **metadata):
    return _tool(part_id, "skill", tokens, input={"skill": name},
                 metadata={"skill_sha256": part_id * 8, **metadata})


def _history():
    """Older copies of alpha, one beta, and the newest alpha in the last turns."""
    turns = [
        [_skill("s0", "alpha")],
        [_skill("s1", "alpha")],
        [_skill("b2", "beta")],
        [_skill("u3", "alpha", tokens=50, skill_unchanged=True)],
        [_skill("s4", "alpha")],
        [_tool("r5", "read", 10)],
    ]
    msgs = []
    for index, parts in enumerate(turns):
        msgs.append(Msg(f"u{index}", "user", [{"type": "text", "text": "go"}]))
        msgs.append(Msg(f"a{index}", "assistant", parts))
    return msgs


async def _prune(monkeypatch, msgs, **kwargs):
    pruned: list[str] = []

    async def surface(*args, **kw):
        return SimpleNamespace(messages=msgs)

    async def update(part_id, data, **kw):
        pruned.append(part_id)

    import session.session as sess
    monkeypatch.setattr("session.agent_event_log.load_canonical_model_surface", surface)
    monkeypatch.setattr(sess, "update_part_data", update)
    await compaction.prune_tool_outputs("s", **kwargs)
    return set(pruned)


async def test_off_keeps_todays_rule_and_never_reads_definitions(tuning, monkeypatch):
    def forbidden(name):
        raise AssertionError("switch off must not read never_prune")

    monkeypatch.setattr(compaction, "_tool_prune_policy", forbidden)
    assert await _prune(monkeypatch, _history(), aggressive=True) == set()


async def test_older_copies_go_and_the_newest_copy_stays(tuning, monkeypatch):
    tuning(prune_stale_skill_copies=True)
    # Not aggressive: the superseded copies alone pass the 20K threshold.
    assert await _prune(monkeypatch, _history()) == {"s0", "s1"}


async def test_a_pointer_never_keeps_an_older_body_alive_by_itself(tuning, monkeypatch):
    tuning(prune_stale_skill_copies=True)
    msgs = _history()[:8]  # the newest alpha body is now s1; u3 is only a pointer
    msgs += [Msg("u9", "user", []), Msg("a9", "assistant", [_tool("r9", "read", 10)])]
    assert await _prune(monkeypatch, msgs, aggressive=True) == {"s0"}


async def test_the_compaction_tail_holds_the_newest_copy(tuning, monkeypatch):
    tuning(prune_stale_skill_copies=True)
    pruned = await _prune(monkeypatch, _history(), aggressive=True, protect_from_id="u4")
    assert {"s0", "s1"} <= pruned and "s4" not in pruned and "b2" not in pruned


async def test_never_prune_is_honoured_only_with_the_switch(tuning, monkeypatch):
    from tool import registry

    async def run(args, ctx):
        raise AssertionError

    keep = ToolInfo(id="keepsake", description="d", parameters=st.SkillArgs, execute=run,
                    never_prune=True)
    monkeypatch.setattr(registry, "_tools", {**registry._tools, "keepsake": keep})
    msgs = []
    for index in range(4):
        msgs.append(Msg(f"u{index}", "user", []))
        msgs.append(Msg(f"a{index}", "assistant", [_tool(f"k{index}", "keepsake", 15_000)]))
    assert await _prune(monkeypatch, msgs, aggressive=True) == {"k0", "k1"}
    tuning(prune_stale_skill_copies=True)
    assert await _prune(monkeypatch, msgs, aggressive=True) == set()


@pytest.mark.parametrize("tool_name", ["StructuredOutput", "mcp_find_tool"])
async def test_tools_built_per_run_stay_prunable(tuning, monkeypatch, tool_name):
    """Their never_prune lives on a per-run definition the pruner cannot see."""
    tuning(prune_stale_skill_copies=True)
    msgs = []
    for index in range(4):
        msgs.append(Msg(f"u{index}", "user", []))
        msgs.append(Msg(f"a{index}", "assistant", [_tool(f"t{index}", tool_name, 15_000)]))
    assert await _prune(monkeypatch, msgs, aggressive=True) == {"t0", "t1"}


async def test_switch_follows_the_sessions_workspace(tuning, monkeypatch):
    tuning(prune_stale_skill_copies=True, workspaces=["ws_internal"])
    import session.session as sess

    workspace = {"id": "ws_customer"}

    async def get_session(session_id, user_id=None):
        return SimpleNamespace(workspace_id=workspace["id"])

    monkeypatch.setattr(sess, "get_session", get_session)
    assert await _prune(monkeypatch, _history()) == set()
    workspace["id"] = "ws_internal"
    assert await _prune(monkeypatch, _history()) == {"s0", "s1"}


VIDEO_SKILL = Path(__file__).resolve().parents[2] / ".openbox/skills/video-production"


def test_video_skill_core_stays_small_and_routes_to_its_detail():
    text = (VIDEO_SKILL / "SKILL.md").read_text(encoding="utf-8")
    assert len(text.encode("utf-8")) <= 10_000
    for name in ("workflow-plan.md", "workflow-produce.md"):
        assert f"references/{name}" in text
        assert (VIDEO_SKILL / "references" / name).is_file()
    # A desktop synced before the split has neither file but a full SKILL.md.
    assert "read `/opt/openbox/skills/video-production/SKILL.md` instead" in text
    headings = "\n".join(
        (VIDEO_SKILL / "references" / name).read_text(encoding="utf-8")
        for name in ("workflow-plan.md", "workflow-produce.md")
    )
    for step in ("## Secretary conversation", "### Keep preparation focused",
                 *(f"### {index}. " for index in range(9)),
                 "## Invalidation and samples", "## What actually goes wrong"):
        assert step in headings, step


def test_manual_desktop_release_lists_every_file_the_skill_reads():
    import re

    doc = Path(__file__).resolve().parents[3] / "docs/WUYING_SANDBOX.md"
    if not doc.is_file():
        pytest.skip("repository docs are not part of this checkout")
    match = re.search(r"names = \((.*?)\n\)", doc.read_text(encoding="utf-8"), re.S)
    listed = set(re.findall(r'"([^"]+)"', match.group(1)))
    text = (VIDEO_SKILL / "SKILL.md").read_text(encoding="utf-8")
    referenced = set(re.findall(r"references/[\w.-]+\.md", text))
    assert referenced <= listed
    assert all((VIDEO_SKILL / name).is_file() for name in listed)


def test_summary_input_keeps_a_reload_note_instead_of_the_body(tuning):
    off = compaction.prune_tool_outputs_view(_history(), aggressive=True)
    assert all(len(part["output"]) > 100 for message in off for part in message.parts
               if part.get("tool") == "skill" and not part["metadata"].get("skill_unchanged"))

    tuning(prune_stale_skill_copies=True)
    view = compaction.prune_tool_outputs_view(_history(), aggressive=True)
    parts = {part["id"]: part for message in view for part in message.parts if part.get("type") == "tool"}
    for part_id in ("s4", "b2"):
        assert "must be loaded again with the skill tool" in parts[part_id]["output"]
    assert "the alpha skill" in parts["s4"]["output"] and "the beta skill" in parts["b2"]["output"]
    for part_id in ("s0", "s1"):
        assert parts[part_id]["state"]["time"]["compacted"]


async def test_summary_view_reads_the_workspace_only_when_the_switch_is_narrowed(tuning, monkeypatch):
    calls = []

    async def fake_get_session(session_id, user_id=None):
        calls.append((session_id, user_id))
        return SimpleNamespace(workspace_id="ws_internal")

    import session.session as session_mod
    monkeypatch.setattr(session_mod, "get_session", fake_get_session)

    tuning()
    assert await compaction._stale_skill_workspace("s1", "u1") is None
    tuning(prune_stale_skill_copies=True)
    assert await compaction._stale_skill_workspace("s1", "u1") is None
    assert calls == []

    tuning(prune_stale_skill_copies=True, workspaces=["ws_internal"])
    assert await compaction._stale_skill_workspace("s1", "u1") == "ws_internal"
    assert await compaction._stale_skill_switch_for_session("s1", "u1") is True
    tuning(prune_stale_skill_copies=True, workspaces=["ws_other"])
    assert await compaction._stale_skill_switch_for_session("s1", "u1") is False
