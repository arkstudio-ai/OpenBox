"""Runtime wiring for private ToolPart provider identities."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from agent import processor as P
from agent.llm import build_responses_input, provider_tool_binding
from agent.loop import _legacy_tool_aliases, _to_llm_messages
from core.config import ProviderConfig
from tool.tool import ToolResult


BINDING_DIGEST = "b" * 64


class _Info:
    id = "msg_runtime_identity"
    error = None


class _Ctx:
    message_id = ""
    part_id = ""


class _NotAborted:
    @staticmethod
    def is_set():
        return False

    @staticmethod
    async def wait():
        await asyncio.Event().wait()


class _Hooks:
    async def wrap_execute(self, tool_name, execute_fn, args, ctx, part_id=""):
        return await execute_fn(args, ctx)


def _stream(events):
    async def generate(**_kwargs):
        for event in events:
            yield event

    return generate


@pytest.fixture(autouse=True)
def _no_external_side_effects(monkeypatch):
    async def noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(P, "update_message_info", noop)
    monkeypatch.setattr(P, "update_session", noop)
    monkeypatch.setattr(P, "create_compaction", noop)
    monkeypatch.setattr(P.bus, "publish", lambda *_args, **_kwargs: None)


async def _run_step(monkeypatch, events, *, saved, tools=None):
    async def capture(part, *_args, **_kwargs):
        saved.append(part.model_copy(deep=True))

    async def execute(_args, _ctx):
        return ToolResult(title="ok", output="done")

    monkeypatch.setattr(P, "save_part", capture)
    monkeypatch.setattr(P, "stream_llm", _stream(events))
    visible = tools or {"read": SimpleNamespace(execute=execute)}
    return await P.process_step(
        session_id="s1",
        user_id="u1",
        session=None,
        agent_def=None,
        system=[],
        llm_messages=[],
        tools=visible,
        model_id="gateway/model-a",
        ctx=_Ctx(),
        hooks=_Hooks(),
        assistant_info=_Info(),
        sandbox=None,
        abort=_NotAborted(),
        doom_loop_history=[],
        execution_lookup={"canonical:read": visible["read"]},
        step_executable_ids=frozenset({"canonical:read"}),
        provider_to_canonical={"read": "canonical:read"},
        provider_binding_digest=BINDING_DIGEST,
        provider_dialect="litellm",
    )


def _assert_bound(part, *, canonical="canonical:read", wire="read", seq=0):
    assert part.canonical_tool_id == canonical
    assert part.wire_tool_name == wire
    assert part.provider_binding_digest == BINDING_DIGEST
    assert part.provider_dialect == "litellm"
    assert part.stream_seq == seq


@pytest.mark.asyncio
async def test_pending_running_and_completed_keep_original_wire_identity(monkeypatch):
    saved = []
    await _run_step(monkeypatch, [
        {"type": "tool_call_start", "index": 0, "tool": "Read", "call_id": "call_1"},
        {
            "type": "tool_call",
            "tool": "read",
            "wire_tool": "Read",
            "args": {"path": "a"},
            "call_id": "call_1",
        },
        {"type": "finish", "reason": "tool_calls", "usage": {}},
    ], saved=saved)

    tool_parts = [part for part in saved if part.type == "tool"]
    assert [part.status.value for part in tool_parts] == [
        "pending",
        "running",
        "completed",
    ]
    for part in tool_parts:
        _assert_bound(part, wire="Read")


@pytest.mark.asyncio
async def test_duplicate_conflict_and_invalid_errors_are_fully_bound(monkeypatch):
    duplicate_saved = []
    duplicate = {
        "type": "tool_call",
        "tool": "read",
        "wire_tool": "read",
        "args": {"path": "a"},
        "call_id": "same",
    }
    await _run_step(monkeypatch, [
        {"type": "tool_call_start", "index": 0, "tool": "read", "call_id": "same"},
        {"type": "tool_call_start", "index": 1, "tool": "read", "call_id": "same"},
        duplicate,
        dict(duplicate),
        {"type": "finish", "reason": "tool_calls", "usage": {}},
    ], saved=duplicate_saved)
    duplicate_error = next(
        part for part in duplicate_saved if part.title == "Duplicate tool call ignored"
    )
    _assert_bound(duplicate_error, seq=1)

    conflict_saved = []
    await _run_step(monkeypatch, [
        {"type": "tool_call", "tool": "read", "args": {"path": "a"}, "call_id": "same"},
        {"type": "tool_call", "tool": "read", "args": {"path": "b"}, "call_id": "same"},
        {"type": "finish", "reason": "tool_calls", "usage": {}},
    ], saved=conflict_saved)
    conflicts = [part for part in conflict_saved if part.title == "Conflicting tool call ids blocked"]
    assert len(conflicts) == 2
    for seq, part in enumerate(conflicts):
        _assert_bound(part, seq=seq)

    invalid_saved = []
    await _run_step(monkeypatch, [
        {
            "type": "tool_call",
            "tool": "unknown_wire",
            "wire_tool": "unknown_wire",
            "args": {},
            "call_id": "bad",
            "invalid": True,
        },
        {"type": "finish", "reason": "tool_calls", "usage": {}},
    ], saved=invalid_saved)
    invalid = next(part for part in reversed(invalid_saved) if part.type == "tool")
    assert invalid.status.value == "error"
    assert invalid.canonical_tool_id.startswith("invalid:v1:")
    _assert_bound(
        invalid,
        canonical=invalid.canonical_tool_id,
        wire="unknown_wire",
    )


def test_provider_binding_covers_route_account_version_beta_config_and_projection():
    secret = "sk-never-persist-this"
    password = "url-password-never-persist-this"

    def binding(**updates):
        options = {
            "api_version": updates.pop("api_version", "2026-08-01"),
            "anthropic_beta": updates.pop("beta", ["tools-v2"]),
            "extra_headers": {
                "Authorization": f"Bearer {secret}",
                "anthropic-beta": "tools-v2",
            },
            "capability_mode": updates.pop("capability_mode", "strict"),
        }
        cfg = SimpleNamespace(
            jwt_secret="local-binding-pepper",
            provider={
                "gateway": ProviderConfig(
                    api_key=updates.pop("api_key", secret),
                    base_url=updates.pop(
                        "base_url",
                        f"https://tenant:{password}@gateway.example/v1?api-key={secret}",
                    ),
                    options=options,
                )
            },
        )
        return provider_tool_binding(
            updates.pop("model", "gateway/model-a"),
            provider_to_canonical=updates.pop(
                "projection", {"read_wire": "canonical:read"}
            ),
            dialect=updates.pop("dialect", "litellm"),
            config=cfg,
        )

    baseline = binding()
    serialized = json.dumps(baseline.model_dump(mode="json"), sort_keys=True)
    assert secret not in serialized
    assert password not in serialized
    assert "Authorization" not in serialized
    assert baseline.api_version == "2026-08-01"
    assert any("anthropic-beta" in item for item in baseline.beta_headers)

    changed = [
        binding(model="gateway/model-b"),
        binding(base_url="https://other.example/v1"),
        binding(api_key="sk-another-account"),
        binding(api_version="2026-09-01"),
        binding(beta=["tools-v3"]),
        binding(capability_mode="permissive"),
        binding(projection={"renamed_wire": "canonical:read"}),
        binding(dialect="responses"),
    ]
    assert all(candidate.digest() != baseline.digest() for candidate in changed)


def test_history_conversion_uses_resolved_wire_and_legacy_collision_stays_ambiguous():
    message = SimpleNamespace(
        role="assistant",
        error=None,
        parts=[{
            "type": "tool",
            "id": "part_1",
            "tool": "old_display_alias",
            "status": "completed",
            "input": {"value": 1},
            "output": "ok",
            "call_id": "call_1",
        }],
    )
    converted = _to_llm_messages(
        [message],
        tool_replay_names={"part_1": "current_provider_wire"},
    )
    assert converted[0]["tool_calls"][0]["function"]["name"] == "current_provider_wire"

    digest_a = "a" * 52
    digest_b = "b" * 52
    canonical_a = f"mcp:v2:{digest_a}"
    canonical_b = f"mcp:v2:{digest_b}"
    aliases = _legacy_tool_aliases(
        {
            f"legacy_alias_{digest_a}": canonical_a,
            f"legacy_alias_{digest_b}": canonical_b,
        },
        {},
    )
    assert aliases["legacy_alias"] == (canonical_a, canonical_b)


def test_native_replay_merges_public_calls_and_private_search_by_stream_seq():
    message = SimpleNamespace(
        id="msg_native_order",
        role="assistant",
        error=None,
        parts=[{
            "type": "tool",
            "id": "part_bash",
            "tool": "bash",
            "status": "completed",
            "input": {"command": "pwd"},
            "output": "/workspace",
            "call_id": "call_bash",
            "stream_seq": 0,
        }],
    )
    converted = _to_llm_messages(
        [message],
        tool_replay_names={"part_bash": "bash"},
        provider_replay_by_message={
            "msg_native_order": [
                {
                    "stream_seq": 1,
                    "item": {
                        "type": "tool_search_call",
                        "execution": "server",
                        "call_id": "search_1",
                    },
                },
                {
                    "stream_seq": 2,
                    "item": {
                        "type": "tool_search_output",
                        "execution": "server",
                        "status": "completed",
                        "call_id": "search_1",
                        "tools": [],
                    },
                },
            ]
        },
    )

    items = build_responses_input(converted)
    assert [item["type"] for item in items] == [
        "function_call",
        "function_call_output",
        "tool_search_call",
        "tool_search_output",
    ]
    assert items[0]["name"] == "bash"
    assert items[0]["call_id"] == items[1]["call_id"] == "fc_bash"
