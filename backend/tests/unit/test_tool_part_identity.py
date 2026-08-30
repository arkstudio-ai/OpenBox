"""Provider-bound ToolPart identity stays private and replay-safe."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select

from db.base import get_db_session
from db.models.message import Message
from db.models.part import PRIVATE_TOOL_PART_FIELDS, Part
from db.models.project import Project
from db.models.session import Session
from db.models.user import User
from db.repository.part_repo import PgPartRepo
from models.message import ToolPartData, ToolStatus
from session.session import get_messages, save_part
from session.tool_part_identity import (
    AmbiguousLegacyToolAlias,
    ToolPartReplayError,
    resolve_tool_part_for_replay,
)


CANONICAL = "mcp:v2:" + "a" * 52


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


async def _seed() -> tuple[str, str, str]:
    suffix = uuid4().hex[:12]
    user_id = f"usr_{suffix}"
    project_id = f"prj_{suffix}"
    session_id = f"ses_{suffix}"
    message_id = f"msg_{suffix}"
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(User(id=user_id, username=user_id, created_at=now, updated_at=now))
        db.add(Project(
            id=project_id,
            user_id=user_id,
            name=project_id,
            slug=project_id,
            created_at=now,
            updated_at=now,
        ))
        db.add(Session(
            id=session_id,
            user_id=user_id,
            project_id=project_id,
            agent="build",
            model="provider/model-a",
            status="idle",
            token_usage={},
            tool_exposure_state={},
            created_at=now,
            updated_at=now,
        ))
        db.add(Message(
            id=message_id,
            session_id=session_id,
            user_id=user_id,
            role="assistant",
            created_at=now,
        ))
    return user_id, session_id, message_id


def _new_part(session_id: str, message_id: str, **updates) -> ToolPartData:
    values = {
        "id": f"part_{uuid4().hex[:12]}",
        "tool": "mcp_report_original",
        "status": ToolStatus.COMPLETED,
        "input": {"range": "today"},
        "output": "ok",
        "call_id": "call_1",
        "session_id": session_id,
        "message_id": message_id,
        "canonical_tool_id": CANONICAL,
        "wire_tool_name": "mcp_report_original",
        "provider_binding_digest": _digest("binding-a"),
        "provider_dialect": "responses",
        "stream_seq": 4,
    }
    values.update(updates)
    return ToolPartData(**values)


@pytest.mark.asyncio
async def test_new_identity_persists_relationally_but_rest_and_sse_stay_public(monkeypatch):
    user_id, session_id, message_id = await _seed()
    published: list[dict] = []
    monkeypatch.setattr(
        "session.session.bus.publish",
        lambda _event, payload: published.append(payload),
    )
    part = _new_part(session_id, message_id)
    await save_part(part, is_new=True, user_id=user_id)

    assert len(published) == 1
    assert not (PRIVATE_TOOL_PART_FIELDS & published[0]["part"].keys())
    assert published[0]["part"]["tool"] == "mcp_report_original"

    async with get_db_session() as db:
        row = await db.get(Part, part.id)
        assert row is not None
        assert row.canonical_tool_id == CANONICAL
        assert row.wire_tool_name == "mcp_report_original"
        assert row.provider_binding_digest == _digest("binding-a")
        assert row.provider_dialect == "responses"
        assert row.stream_seq == 4
        assert not (PRIVATE_TOOL_PART_FIELDS & row.data.keys())

    snapshot = await get_messages(session_id, user_id=user_id)
    public_part = snapshot[0].parts[0].model_dump()
    assert public_part["tool"] == "mcp_report_original"
    assert not (PRIVATE_TOOL_PART_FIELDS & public_part.keys())
    repo_part = await PgPartRepo().get(part.id)
    assert repo_part is not None
    assert not (PRIVATE_TOOL_PART_FIELDS & repo_part.keys())


@pytest.mark.asyncio
async def test_same_binding_reuses_original_wire_and_switch_remaps_from_canonical():
    user_id, session_id, message_id = await _seed()
    part = _new_part(session_id, message_id)
    await save_part(part, is_new=True, user_id=user_id)

    same = await resolve_tool_part_for_replay(
        part_id=part.id,
        session_id=session_id,
        user_id=user_id,
        current_binding_digest=_digest("binding-a"),
        current_provider_dialect="responses",
        # A rebuilt map may now prefer another collision suffix. Exact binding
        # replay must still use the original immutable request name.
        current_wire_by_canonical={CANONICAL: "mcp_report_rebuilt"},
    )
    assert same.same_binding is True
    assert same.wire_tool_name == "mcp_report_original"
    assert same.canonical_tool_id == CANONICAL

    switched = await resolve_tool_part_for_replay(
        part_id=part.id,
        session_id=session_id,
        user_id=user_id,
        current_binding_digest=_digest("binding-b"),
        current_provider_dialect="litellm",
        current_wire_by_canonical={CANONICAL: "mcp_report_target"},
    )
    assert switched.same_binding is False
    assert switched.wire_tool_name == "mcp_report_target"
    assert switched.original_wire_tool_name == "mcp_report_original"

    with pytest.raises(ToolPartReplayError, match="unavailable"):
        await resolve_tool_part_for_replay(
            part_id=part.id,
            session_id=session_id,
            user_id=user_id,
            current_binding_digest=_digest("binding-c"),
            current_provider_dialect="litellm",
            current_wire_by_canonical={},
        )


async def _insert_legacy_part(
    user_id: str,
    session_id: str,
    message_id: str,
    alias: str,
) -> str:
    part_id = f"part_{uuid4().hex[:12]}"
    async with get_db_session() as db:
        db.add(Part(
            id=part_id,
            message_id=message_id,
            session_id=session_id,
            user_id=user_id,
            type="tool",
            data={
                "type": "tool",
                "id": part_id,
                "tool": alias,
                "status": "completed",
                "call_id": "old_call",
                "session_id": session_id,
                "message_id": message_id,
            },
            created_at=datetime.now(timezone.utc),
        ))
    return part_id


@pytest.mark.asyncio
async def test_legacy_unique_alias_lazy_backfills_hidden_identity():
    user_id, session_id, message_id = await _seed()
    part_id = await _insert_legacy_part(
        user_id, session_id, message_id, "legacy_mcp_report"
    )
    replay = await resolve_tool_part_for_replay(
        part_id=part_id,
        session_id=session_id,
        user_id=user_id,
        current_binding_digest=_digest("binding-current"),
        current_provider_dialect="responses",
        current_wire_by_canonical={CANONICAL: "mcp_report_current"},
        legacy_aliases={"legacy_mcp_report": CANONICAL},
    )
    assert replay.identity_source == "legacy_unique_alias"
    assert replay.canonical_tool_id == CANONICAL
    assert replay.wire_tool_name == "mcp_report_current"
    assert replay.stream_seq == 0

    async with get_db_session() as db:
        row = await db.get(Part, part_id)
        assert row is not None
        assert row.canonical_tool_id == CANONICAL
        assert row.wire_tool_name == "mcp_report_current"
        assert row.data["tool"] == "legacy_mcp_report"
        assert not (PRIVATE_TOOL_PART_FIELDS & row.data.keys())


@pytest.mark.asyncio
async def test_multiple_legacy_calls_backfill_in_deterministic_tool_order():
    user_id, session_id, message_id = await _seed()
    first = await _insert_legacy_part(user_id, session_id, message_id, "legacy_first")
    second = await _insert_legacy_part(user_id, session_id, message_id, "legacy_second")
    canonical_second = "mcp:v2:" + "b" * 52
    common = {
        "session_id": session_id,
        "user_id": user_id,
        "current_binding_digest": _digest("binding-current"),
        "current_provider_dialect": "responses",
        "current_wire_by_canonical": {
            CANONICAL: "wire_first",
            canonical_second: "wire_second",
        },
        "legacy_aliases": {
            "legacy_first": CANONICAL,
            "legacy_second": canonical_second,
        },
    }
    first_replay = await resolve_tool_part_for_replay(part_id=first, **common)
    second_replay = await resolve_tool_part_for_replay(part_id=second, **common)
    assert (first_replay.stream_seq, second_replay.stream_seq) == (0, 1)


@pytest.mark.asyncio
async def test_legacy_alias_collision_and_partial_identity_fail_closed():
    user_id, session_id, message_id = await _seed()
    part_id = await _insert_legacy_part(user_id, session_id, message_id, "collision")
    with pytest.raises(AmbiguousLegacyToolAlias, match="unknown or ambiguous"):
        await resolve_tool_part_for_replay(
            part_id=part_id,
            session_id=session_id,
            user_id=user_id,
            current_binding_digest=_digest("binding-current"),
            current_provider_dialect="responses",
            current_wire_by_canonical={
                CANONICAL: "wire_a",
                "mcp:v2:" + "b" * 52: "wire_b",
            },
            legacy_aliases={
                "collision": [CANONICAL, "mcp:v2:" + "b" * 52]
            },
        )
    async with get_db_session() as db:
        row = await db.get(Part, part_id)
        assert row is not None
        assert row.canonical_tool_id is None

    partial = _new_part(
        session_id,
        message_id,
        wire_tool_name=None,
    )
    with pytest.raises(ToolPartReplayError, match="partial"):
        await save_part(partial, is_new=True, user_id=user_id)
    async with get_db_session() as db:
        assert await db.get(Part, partial.id) is None


@pytest.mark.asyncio
async def test_current_provider_wire_collision_and_cross_owner_replay_fail_closed():
    user_id, session_id, message_id = await _seed()
    part = _new_part(session_id, message_id)
    await save_part(part, is_new=True, user_id=user_id)
    with pytest.raises(ToolPartReplayError, match="collision"):
        await resolve_tool_part_for_replay(
            part_id=part.id,
            session_id=session_id,
            user_id=user_id,
            current_binding_digest=_digest("binding-b"),
            current_provider_dialect="litellm",
            current_wire_by_canonical={
                CANONICAL: "same_wire",
                "mcp:v2:" + "b" * 52: "same_wire",
            },
        )
    with pytest.raises(LookupError, match="session not found"):
        await resolve_tool_part_for_replay(
            part_id=part.id,
            session_id=session_id,
            user_id="another-user",
            current_binding_digest=_digest("binding-a"),
            current_provider_dialect="responses",
            current_wire_by_canonical={CANONICAL: "mcp_report_original"},
        )
