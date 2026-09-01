"""State/event atomicity and API isolation for deferred tool exposure."""
from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from db.base import get_db_session
from db.models.internal_part import InternalPart
from db.models.message import Message
from db.models.part import Part
from db.models.project import Project
from db.models.session import Session as SessionORM
from db.models.user import User
from db.repository.session_repo import PgSessionRepo
from session.fork import fork_session
from session.internal_parts import (
    PROVIDER_TRANSCRIPT_KIND,
    TOOL_REVEAL_KIND,
    ProviderCapabilityBinding,
    ToolRevealEvent,
    commit_tool_reveal,
    commit_tool_reveals,
    get_provider_replay_parts,
    get_provider_replay_parts_for_binding,
    get_provider_fallback_status,
    get_valid_revealed_ids,
    rebuild_tool_exposure_state,
    save_internal_part,
    set_provider_fallback_status,
)
from session.session import delete_messages_from, delete_session, get_messages, get_session


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _binding(*, account: str = "acct-a", model: str = "gpt-test") -> ProviderCapabilityBinding:
    return ProviderCapabilityBinding(
        provider="openai",
        endpoint="https://api.example.test/v1",
        account_id=account,
        api_version="2026-08-01",
        model=model,
        dialect="responses",
        beta_headers=("tools-v2",),
    )


async def _seed_scope(
    *,
    message_count: int = 1,
    reveal_origins: bool = False,
) -> tuple[str, str, list[str], list[str]]:
    suffix = uuid4().hex[:12]
    user_id = f"usr_{suffix}"
    project_id = f"prj_{suffix}"
    session_id = f"ses_{suffix}"
    message_ids = [f"msg_{suffix}_{index}" for index in range(message_count)]
    part_ids = [f"part_{suffix}_{index}" for index in range(message_count)]
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(
            User(
                id=user_id,
                username=user_id,
                created_at=now,
                updated_at=now,
            )
        )
        db.add(
            Project(
                id=project_id,
                user_id=user_id,
                name=project_id,
                slug=project_id,
                created_at=now,
                updated_at=now,
            )
        )
        db.add(
            SessionORM(
                id=session_id,
                user_id=user_id,
                project_id=project_id,
                title="private-state-test",
                agent="build",
                model="gpt-test",
                status="idle",
                token_usage={},
                tool_exposure_state={},
                created_at=now,
                updated_at=now,
            )
        )
        for index, (message_id, part_id) in enumerate(zip(message_ids, part_ids)):
            created = now + timedelta(microseconds=index)
            db.add(
                Message(
                    id=message_id,
                    session_id=session_id,
                    user_id=user_id,
                    role="user" if index == 0 else "assistant",
                    parent_id=message_ids[0] if index else None,
                    created_at=created,
                )
            )
            part_type = "tool" if reveal_origins else "text"
            part_data = (
                {
                    "type": "tool",
                    "id": part_id,
                    "message_id": message_id,
                    "session_id": session_id,
                    "tool": "capability_search",
                    "status": "completed",
                    "input": {"query": "test"},
                    "output": "read",
                    "call_id": f"call-{index}",
                }
                if reveal_origins
                else {
                    "type": "text",
                    "id": part_id,
                    "message_id": message_id,
                    "session_id": session_id,
                    "text": f"public-{index}",
                }
            )
            db.add(
                Part(
                    id=part_id,
                    message_id=message_id,
                    session_id=session_id,
                    user_id=user_id,
                    type=part_type,
                    data=part_data,
                    created_at=created,
                )
            )
    return user_id, session_id, message_ids, part_ids


def _event(
    user_id: str,
    session_id: str,
    message_id: str,
    part_id: str,
    tool_id: str,
    *,
    generation: str = "generation-1",
    agent_id: str = "build",
    stream_seq: int = 1,
) -> ToolRevealEvent:
    return ToolRevealEvent(
        session_id=session_id,
        user_id=user_id,
        message_id=message_id,
        origin_part_id=part_id,
        agent_id=agent_id,
        canonical_tool_id=tool_id,
        schema_digest=_digest(f"schema:{tool_id}"),
        catalog_generation=generation,
        evidence_source="portable",
        stream_seq=stream_seq,
    )


@pytest.mark.asyncio
async def test_internal_provider_parts_are_owner_bound_and_absent_from_public_api(monkeypatch):
    user_id, session_id, messages, _ = await _seed_scope()
    binding = _binding()
    published: list[tuple] = []
    monkeypatch.setattr("bus.bus.publish", lambda *args, **kwargs: published.append((args, kwargs)))
    saved = await save_internal_part(
        session_id=session_id,
        user_id=user_id,
        message_id=messages[0],
        kind=PROVIDER_TRANSCRIPT_KIND,
        data={"opaque": "provider-only", "token": "secret"},
        binding=binding,
        response_chain_id="chain-1",
        stream_seq=3,
        idempotency_key="event-1",
    )
    duplicate = await save_internal_part(
        session_id=session_id,
        user_id=user_id,
        message_id=messages[0],
        kind=PROVIDER_TRANSCRIPT_KIND,
        data={"opaque": "provider-only", "token": "secret"},
        binding=binding,
        response_chain_id="chain-1",
        stream_seq=3,
        idempotency_key="event-1",
    )

    assert duplicate.id == saved.id
    assert published == []
    with pytest.raises(ValueError, match="idempotency conflict"):
        await save_internal_part(
            session_id=session_id,
            user_id=user_id,
            message_id=messages[0],
            kind=PROVIDER_TRANSCRIPT_KIND,
            data={"opaque": "conflicting-retry"},
            binding=binding,
            response_chain_id="chain-1",
            stream_seq=3,
            idempotency_key="event-1",
        )
    public_messages = await get_messages(session_id, user_id=user_id)
    assert len(public_messages) == 1
    assert [part.type for part in public_messages[0].parts] == ["text"]
    assert "provider-only" not in public_messages[0].model_dump_json()

    public_session = await get_session(session_id, user_id=user_id)
    assert public_session is not None
    assert "tool_exposure_state" not in public_session.model_dump()
    assert "tool_exposure_state" not in public_session.model_dump_json()
    assert "tool_exposure_state" not in (
        await PgSessionRepo().get(session_id, user_id)
    )

    replay = await get_provider_replay_parts(
        session_id=session_id,
        user_id=user_id,
        binding=binding,
        response_chain_id="chain-1",
    )
    assert [row.data for row in replay] == [{"opaque": "provider-only"}]
    assert await get_provider_replay_parts(
        session_id=session_id,
        user_id=user_id,
        binding=_binding(account="acct-b"),
        response_chain_id="chain-1",
    ) == []
    assert await get_provider_replay_parts(
        session_id=session_id,
        user_id="another-user",
        binding=binding,
        response_chain_id="chain-1",
    ) == []

    with pytest.raises(LookupError, match="session not found"):
        await save_internal_part(
            session_id=session_id,
            user_id="another-user",
            message_id=messages[0],
            kind=PROVIDER_TRANSCRIPT_KIND,
            data={},
            binding=binding,
            response_chain_id="chain-2",
            stream_seq=0,
        )


@pytest.mark.asyncio
async def test_provider_capability_fallback_is_binding_scoped_sticky_and_expires():
    user_id, session_id, _, _ = await _seed_scope()
    key_a = _digest("binding-a")
    key_b = _digest("binding-b")
    now = datetime.now(timezone.utc)
    await set_provider_fallback_status(
        session_id=session_id,
        user_id=user_id,
        capability_key_digest=key_a,
        status="unsupported",
        ttl_seconds=60,
        reason="pre_stream_http_400",
        now=now,
    )

    status = await get_provider_fallback_status(
        session_id=session_id,
        user_id=user_id,
        capability_key_digest=key_a,
        now=now + timedelta(seconds=30),
    )
    assert status is not None
    assert status[0] == "unsupported"
    assert status[2] == "pre_stream_http_400"
    assert await get_provider_fallback_status(
        session_id=session_id,
        user_id=user_id,
        capability_key_digest=key_b,
        now=now + timedelta(seconds=30),
    ) is None
    assert await get_provider_fallback_status(
        session_id=session_id,
        user_id=user_id,
        capability_key_digest=key_a,
        now=now + timedelta(seconds=61),
    ) is None


@pytest.mark.asyncio
async def test_binding_wide_replay_query_never_crosses_account():
    user_id, session_id, messages, _ = await _seed_scope(message_count=2)
    for index, message_id in enumerate(messages):
        await save_internal_part(
            session_id=session_id,
            user_id=user_id,
            message_id=message_id,
            kind=PROVIDER_TRANSCRIPT_KIND,
            data={"type": "tool_search_call" if index == 0 else "tool_search_output"},
            binding=_binding(),
            response_chain_id=f"chain-{index}",
            stream_seq=index,
            idempotency_key=f"event-{index}",
        )
    assert len(await get_provider_replay_parts_for_binding(
        session_id=session_id,
        user_id=user_id,
        binding=_binding(),
    )) == 2
    assert await get_provider_replay_parts_for_binding(
        session_id=session_id,
        user_id=user_id,
        binding=_binding(account="acct-b"),
    ) == []


@pytest.mark.asyncio
async def test_native_replay_isolated_by_full_capability_key_generation():
    user_id, session_id, messages, _ = await _seed_scope()
    binding = _binding()
    old_catalog_key = _digest("native-config:catalog-schema-v1")
    changed_catalog_key = _digest("native-config:catalog-schema-v2")
    await save_internal_part(
        session_id=session_id,
        user_id=user_id,
        message_id=messages[0],
        kind=PROVIDER_TRANSCRIPT_KIND,
        data={
            "type": "tool_search_call",
            "execution": "server",
            "call_id": "search_1",
        },
        binding=binding,
        capability_key_digest=old_catalog_key,
        response_chain_id="chain-schema-v1",
        stream_seq=0,
    )

    assert len(await get_provider_replay_parts_for_binding(
        session_id=session_id,
        user_id=user_id,
        binding=binding,
        capability_key_digest=old_catalog_key,
    )) == 1
    assert await get_provider_replay_parts_for_binding(
        session_id=session_id,
        user_id=user_id,
        binding=binding,
        capability_key_digest=changed_catalog_key,
    ) == []


@pytest.mark.asyncio
async def test_reveal_commit_is_idempotent_set_union_and_scoped():
    user_id, session_id, messages, parts = await _seed_scope(
        message_count=2,
        reveal_origins=True,
    )
    first, second = await asyncio.gather(
        commit_tool_reveal(_event(user_id, session_id, messages[0], parts[0], "read")),
        commit_tool_reveal(
            _event(user_id, session_id, messages[1], parts[1], "grep", stream_seq=2)
        ),
    )
    assert {first.origin_seq, second.origin_seq} == {1, 2}

    retry = await commit_tool_reveal(
        _event(user_id, session_id, messages[0], parts[0], "read")
    )
    assert retry.created is False
    async with get_db_session() as db:
        count = (
            await db.execute(
                select(func.count()).select_from(InternalPart).where(
                    InternalPart.session_id == session_id,
                    InternalPart.kind == "tool_reveal",
                )
            )
        ).scalar_one()
        state = (
            await db.execute(
                select(SessionORM.tool_exposure_state).where(SessionORM.id == session_id)
            )
        ).scalar_one()
    assert count == 2
    assert set(state["agents"]["build"]["revealed"]) == {"read", "grep"}
    assert state["next_origin_seq"] == 3

    assert await get_valid_revealed_ids(
        session_id=session_id,
        user_id=user_id,
        agent_id="build",
        catalog_generation="generation-1",
        schema_digests={"read": _digest("schema:read"), "grep": _digest("schema:grep")},
    ) == frozenset({"read", "grep"})
    assert await get_valid_revealed_ids(
        session_id=session_id,
        user_id=user_id,
        agent_id="plan",
        catalog_generation="generation-1",
        schema_digests={"read": _digest("schema:read")},
    ) == frozenset()
    assert await get_valid_revealed_ids(
        session_id=session_id,
        user_id=user_id,
        agent_id="build",
        catalog_generation="generation-2",
        schema_digests={"read": _digest("schema:read")},
    ) == frozenset()
    async with get_db_session() as db:
        preserved_state = (
            await db.execute(
                select(SessionORM.tool_exposure_state).where(SessionORM.id == session_id)
            )
        ).scalar_one()
    # An unordered generation mismatch is fail-closed, not destructive: the
    # response could have been fetched before another worker committed g1.
    assert set(preserved_state["agents"]["build"]["revealed"]) == {
        "read",
        "grep",
    }
    assert await get_valid_revealed_ids(
        session_id=session_id,
        user_id=user_id,
        agent_id="build",
        catalog_generation="generation-1",
        schema_digests={"read": _digest("changed")},
    ) == frozenset()


@pytest.mark.asyncio
async def test_cold_unavailable_catalogue_does_not_prune_and_reconnect_restores():
    user_id, session_id, messages, parts = await _seed_scope(
        reveal_origins=True,
    )
    await commit_tool_reveal(
        _event(user_id, session_id, messages[0], parts[0], "read")
    )

    async with get_db_session() as db:
        before_state = (
            await db.execute(
                select(SessionORM.tool_exposure_state).where(
                    SessionORM.id == session_id
                )
            )
        ).scalar_one()
        before_count = (
            await db.execute(
                select(func.count()).select_from(InternalPart).where(
                    InternalPart.session_id == session_id,
                    InternalPart.kind == TOOL_REVEAL_KIND,
                )
            )
        ).scalar_one()

    # A cold process has no remote MCP definitions and therefore assembles an
    # incomplete generation. That fail-small step must not treat the absence as
    # an authoritative deletion.
    assert await get_valid_revealed_ids(
        session_id=session_id,
        user_id=user_id,
        agent_id="build",
        catalog_generation="cold-incomplete-generation",
        schema_digests={},
        catalogue_availability="unavailable",
    ) == frozenset()

    async with get_db_session() as db:
        after_state = (
            await db.execute(
                select(SessionORM.tool_exposure_state).where(
                    SessionORM.id == session_id
                )
            )
        ).scalar_one()
        after_count = (
            await db.execute(
                select(func.count()).select_from(InternalPart).where(
                    InternalPart.session_id == session_id,
                    InternalPart.kind == TOOL_REVEAL_KIND,
                )
            )
        ).scalar_one()

    assert after_state == before_state
    assert after_count == before_count == 1
    assert await get_valid_revealed_ids(
        session_id=session_id,
        user_id=user_id,
        agent_id="build",
        catalog_generation="generation-1",
        schema_digests={"read": _digest("schema:read")},
        catalogue_availability="available",
    ) == frozenset({"read"})


@pytest.mark.asyncio
async def test_stale_older_generation_cannot_delete_newer_worker_reveal():
    user_id, session_id, messages, parts = await _seed_scope(
        reveal_origins=True,
    )
    await commit_tool_reveal(
        _event(
            user_id,
            session_id,
            messages[0],
            parts[0],
            "read",
            generation="generation-2",
        )
    )

    assert await get_valid_revealed_ids(
        session_id=session_id,
        user_id=user_id,
        agent_id="build",
        catalog_generation="generation-1",
        schema_digests={},
        catalogue_availability="stale",
    ) == frozenset()

    async with get_db_session() as db:
        count = (
            await db.execute(
                select(func.count()).select_from(InternalPart).where(
                    InternalPart.session_id == session_id,
                    InternalPart.kind == TOOL_REVEAL_KIND,
                )
            )
        ).scalar_one()
        state = (
            await db.execute(
                select(SessionORM.tool_exposure_state).where(
                    SessionORM.id == session_id
                )
            )
        ).scalar_one()
    assert count == 1
    assert state["agents"]["build"]["catalog_generation"] == "generation-2"
    assert set(state["agents"]["build"]["revealed"]) == {"read"}
    assert await get_valid_revealed_ids(
        session_id=session_id,
        user_id=user_id,
        agent_id="build",
        catalog_generation="generation-2",
        schema_digests={"read": _digest("schema:read")},
        catalogue_availability="available",
    ) == frozenset({"read"})


@pytest.mark.asyncio
async def test_late_available_g1_response_cannot_replace_committed_g2():
    user_id, session_id, messages, parts = await _seed_scope(
        message_count=2,
        reveal_origins=True,
    )
    await commit_tool_reveal(
        _event(user_id, session_id, messages[0], parts[0], "read")
    )
    g1_response_captured = asyncio.Event()
    resume_g1_worker = asyncio.Event()

    async def worker_a_with_earlier_g1_response():
        g1_response_captured.set()
        await resume_g1_worker.wait()
        return await get_valid_revealed_ids(
            session_id=session_id,
            user_id=user_id,
            agent_id="build",
            catalog_generation="generation-1",
            schema_digests={"read": _digest("schema:read")},
            catalogue_availability="available",
        )

    worker_a = asyncio.create_task(worker_a_with_earlier_g1_response())
    await g1_response_captured.wait()
    await commit_tool_reveal(
        _event(
            user_id,
            session_id,
            messages[1],
            parts[1],
            "write",
            generation="generation-2",
            stream_seq=2,
        )
    )
    resume_g1_worker.set()

    assert await worker_a == frozenset()
    async with get_db_session() as db:
        rows = (
            await db.execute(
                select(InternalPart).where(
                    InternalPart.session_id == session_id,
                    InternalPart.kind == TOOL_REVEAL_KIND,
                )
            )
        ).scalars().all()
        state = (
            await db.execute(
                select(SessionORM.tool_exposure_state).where(
                    SessionORM.id == session_id
                )
            )
        ).scalar_one()
    assert {row.data["catalog_generation"] for row in rows} == {
        "generation-1",
        "generation-2",
    }
    assert state["agents"]["build"]["catalog_generation"] == "generation-2"
    assert set(state["agents"]["build"]["revealed"]) == {"write"}
    assert await get_valid_revealed_ids(
        session_id=session_id,
        user_id=user_id,
        agent_id="build",
        catalog_generation="generation-2",
        schema_digests={"write": _digest("schema:write")},
        catalogue_availability="available",
    ) == frozenset({"write"})


@pytest.mark.asyncio
async def test_only_reserved_portable_or_verified_native_origin_can_reveal():
    user_id, session_id, messages, parts = await _seed_scope()
    async with get_db_session() as db:
        row = await db.get(Part, parts[0])
        assert row is not None
        row.type = "tool"
        row.data = {
            "type": "tool",
            "tool": "skill",
            "status": "completed",
            "metadata": {"revealed_ids": ["read"]},
        }
    forged = _event(user_id, session_id, messages[0], parts[0], "read")
    with pytest.raises(ValueError, match="invalid reveal origin"):
        await commit_tool_reveal(forged)

    binding = _binding()
    reference = await save_internal_part(
        session_id=session_id,
        user_id=user_id,
        message_id=messages[0],
        kind=PROVIDER_TRANSCRIPT_KIND,
        data={"type": "tool_revealed", "provider_reference": "opaque"},
        binding=binding,
        response_chain_id="native-chain",
        stream_seq=1,
        idempotency_key="native-reference-1",
    )
    native = ToolRevealEvent(
        session_id=session_id,
        user_id=user_id,
        message_id=messages[0],
        origin_part_id=reference.id,
        agent_id="build",
        canonical_tool_id="read",
        schema_digest=_digest("schema:read"),
        catalog_generation="generation-1",
        evidence_source="native",
        stream_seq=2,
        capability_key_digest=binding.digest(),
        response_chain_id="native-chain",
    )
    result = await commit_tool_reveal(native)
    assert result.created is True
    assert set(result.state["agents"]["build"]["revealed"]) == {"read"}


@pytest.mark.asyncio
async def test_reveal_lru_ttl_and_agent_generation_projection():
    user_id, session_id, messages, parts = await _seed_scope(reveal_origins=True)
    for index, tool_id in enumerate(("one", "two", "three"), start=1):
        await commit_tool_reveal(
            _event(
                user_id,
                session_id,
                messages[0],
                parts[0],
                tool_id,
                stream_seq=index,
            ),
            max_reveals=2,
            ttl_seconds=60,
        )
    state = await rebuild_tool_exposure_state(session_id, user_id, max_reveals=2)
    assert set(state["agents"]["build"]["revealed"]) == {"two", "three"}

    expired = await rebuild_tool_exposure_state(
        session_id,
        user_id,
        now=datetime.now(timezone.utc) + timedelta(minutes=2),
        max_reveals=2,
    )
    assert expired["agents"] == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stage",
    ["before_insert", "after_insert", "before_projection", "before_commit"],
)
async def test_reveal_event_and_projection_rollback_together(stage: str):
    user_id, session_id, messages, parts = await _seed_scope(reveal_origins=True)

    def failpoint(current: str) -> None:
        if current == stage:
            raise RuntimeError(f"fault:{stage}")

    with pytest.raises(RuntimeError, match=f"fault:{stage}"):
        await commit_tool_reveal(
            _event(user_id, session_id, messages[0], parts[0], "read"),
            _fault_injector=failpoint,
        )

    async with get_db_session() as db:
        count = (
            await db.execute(
                select(func.count()).select_from(InternalPart).where(
                    InternalPart.session_id == session_id
                )
            )
        ).scalar_one()
        raw = (
            await db.execute(
                select(SessionORM.tool_exposure_state).where(SessionORM.id == session_id)
            )
        ).scalar_one()
    assert count == 0
    assert raw in ({}, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["after_insert:1", "before_projection:1"])
async def test_reveal_batch_second_item_fault_rolls_back_every_row_and_state(stage: str):
    user_id, session_id, messages, parts = await _seed_scope(reveal_origins=True)
    events = (
        _event(user_id, session_id, messages[0], parts[0], "read", stream_seq=0),
        _event(user_id, session_id, messages[0], parts[0], "grep", stream_seq=1),
    )

    def failpoint(current: str) -> None:
        if current == stage:
            raise RuntimeError(f"fault:{stage}")

    with pytest.raises(RuntimeError, match=f"fault:{stage}"):
        await commit_tool_reveals(events, _fault_injector=failpoint)

    async with get_db_session() as db:
        count = (
            await db.execute(
                select(func.count()).select_from(InternalPart).where(
                    InternalPart.session_id == session_id,
                    InternalPart.kind == "tool_reveal",
                )
            )
        ).scalar_one()
        raw = (
            await db.execute(
                select(SessionORM.tool_exposure_state).where(SessionORM.id == session_id)
            )
        ).scalar_one()
    assert count == 0
    assert raw in ({}, None)


@pytest.mark.asyncio
async def test_reveal_batch_retry_and_in_batch_duplicate_are_idempotent():
    user_id, session_id, messages, parts = await _seed_scope(reveal_origins=True)
    read = _event(user_id, session_id, messages[0], parts[0], "read", stream_seq=0)
    grep = _event(user_id, session_id, messages[0], parts[0], "grep", stream_seq=1)

    committed = await commit_tool_reveals((read, read, grep))
    assert [result.created for result in committed] == [True, False, True]
    assert committed[0].origin_seq == committed[1].origin_seq
    assert {committed[0].origin_seq, committed[2].origin_seq} == {1, 2}

    retried = await commit_tool_reveals((read, grep))
    assert [result.created for result in retried] == [False, False]
    assert [result.origin_seq for result in retried] == [
        committed[0].origin_seq,
        committed[2].origin_seq,
    ]

    async with get_db_session() as db:
        count = (
            await db.execute(
                select(func.count()).select_from(InternalPart).where(
                    InternalPart.session_id == session_id,
                    InternalPart.kind == "tool_reveal",
                )
            )
        ).scalar_one()
        state = (
            await db.execute(
                select(SessionORM.tool_exposure_state).where(SessionORM.id == session_id)
            )
        ).scalar_one()
    assert count == 2
    assert set(state["agents"]["build"]["revealed"]) == {"read", "grep"}
    assert state["next_origin_seq"] == 3


@pytest.mark.asyncio
async def test_reveal_batch_conflict_or_invalid_second_origin_writes_nothing():
    user_id, session_id, messages, parts = await _seed_scope(reveal_origins=True)
    first = _event(user_id, session_id, messages[0], parts[0], "read", stream_seq=0)
    conflicting_retry = first.model_copy(update={"stream_seq": 1})
    with pytest.raises(ValueError, match="idempotency conflict"):
        await commit_tool_reveals((first, conflicting_retry))

    invalid_second = _event(
        user_id,
        session_id,
        messages[0],
        "part_missing",
        "grep",
        stream_seq=1,
    )
    with pytest.raises(LookupError, match="origin part not found"):
        await commit_tool_reveals((first, invalid_second))

    async with get_db_session() as db:
        count = (
            await db.execute(
                select(func.count()).select_from(InternalPart).where(
                    InternalPart.session_id == session_id,
                    InternalPart.kind == "tool_reveal",
                )
            )
        ).scalar_one()
        raw = (
            await db.execute(
                select(SessionORM.tool_exposure_state).where(SessionORM.id == session_id)
            )
        ).scalar_one()
    assert count == 0
    assert raw in ({}, None)


@pytest.mark.asyncio
async def test_regenerate_deletes_branch_events_and_rebuilds_survivors():
    user_id, session_id, messages, parts = await _seed_scope(
        message_count=3,
        reveal_origins=True,
    )
    await commit_tool_reveal(_event(user_id, session_id, messages[0], parts[0], "read"))
    await commit_tool_reveal(
        _event(user_id, session_id, messages[1], parts[1], "web_search", stream_seq=2)
    )

    survivor = await delete_messages_from(
        session_id,
        messages[1],
        user_id=user_id,
    )
    assert survivor == messages[0]
    state = await rebuild_tool_exposure_state(session_id, user_id)
    assert set(state["agents"]["build"]["revealed"]) == {"read"}
    async with get_db_session() as db:
        private_messages = set((await db.execute(
            select(InternalPart.message_id).where(InternalPart.session_id == session_id)
        )).scalars().all())
    assert private_messages == {messages[0]}


@pytest.mark.asyncio
async def test_delete_vs_reveal_both_lock_interleavings_do_not_resurrect(monkeypatch):
    import session.internal_parts as internal

    # Interleaving A: reveal owns the session lock first; delete runs second
    # and must remove both the message and the freshly committed event.
    user_a, session_a, messages_a, parts_a = await _seed_scope(
        message_count=2,
        reveal_origins=True,
    )
    entered = asyncio.Event()
    release = asyncio.Event()
    original_require = internal._require_origin_part

    async def paused_require(*args, **kwargs):
        await original_require(*args, **kwargs)
        entered.set()
        await release.wait()

    with monkeypatch.context() as scoped:
        scoped.setattr(internal, "_require_origin_part", paused_require)
        reveal_task = asyncio.create_task(
            commit_tool_reveal(
                _event(user_a, session_a, messages_a[1], parts_a[1], "web_search")
            )
        )
        await entered.wait()
        delete_task = asyncio.create_task(
            delete_messages_from(session_a, messages_a[1], user_id=user_a)
        )
        release.set()
        await reveal_task
        await delete_task
    state_a = await rebuild_tool_exposure_state(session_a, user_a)
    assert state_a["agents"] == {}

    # Interleaving B: delete owns the same lock first.  A delayed reveal must
    # fail its ownership/origin check after deletion, never recreating state.
    user_b, session_b, messages_b, parts_b = await _seed_scope(
        message_count=2,
        reveal_origins=True,
    )
    entered = asyncio.Event()
    release = asyncio.Event()
    original_delete = internal.delete_internal_parts_for_messages_locked

    async def paused_delete(*args, **kwargs):
        result = await original_delete(*args, **kwargs)
        entered.set()
        await release.wait()
        return result

    with monkeypatch.context() as scoped:
        scoped.setattr(internal, "delete_internal_parts_for_messages_locked", paused_delete)
        delete_task = asyncio.create_task(
            delete_messages_from(session_b, messages_b[1], user_id=user_b)
        )
        await entered.wait()
        reveal_task = asyncio.create_task(
            commit_tool_reveal(
                _event(user_b, session_b, messages_b[1], parts_b[1], "web_search")
            )
        )
        release.set()
        await delete_task
        with pytest.raises(LookupError, match="message not found"):
            await reveal_task
    state_b = await rebuild_tool_exposure_state(session_b, user_b)
    assert state_b["agents"] == {}


@pytest.mark.asyncio
async def test_fork_drops_private_state_and_session_delete_clears_it():
    user_id, session_id, messages, parts = await _seed_scope(
        message_count=2,
        reveal_origins=True,
    )
    await commit_tool_reveal(_event(user_id, session_id, messages[0], parts[0], "read"))
    # Forks intentionally reject an open Assistant tail. This legacy fixture
    # predates Agent events, so make its synthetic turn truthfully complete
    # before the one-time Surface seed is captured.
    async with get_db_session() as db:
        assistant = await db.get(Message, messages[1])
        assert assistant is not None
        assistant.finish = "stop"
    child = await fork_session(session_id, user_id=user_id)
    child_session = await get_session(child.id, user_id=user_id)
    assert child_session is not None
    assert child_session.tool_exposure_state == {}
    async with get_db_session() as db:
        child_internal = (
            await db.execute(
                select(func.count()).select_from(InternalPart).where(
                    InternalPart.session_id == child.id
                )
            )
        ).scalar_one()
    assert child_internal == 0

    assert await delete_session(session_id, user_id=user_id) is True
    async with get_db_session() as db:
        source_internal = (
            await db.execute(
                select(func.count()).select_from(InternalPart).where(
                    InternalPart.session_id == session_id
                )
            )
        ).scalar_one()
        source_state = (
            await db.execute(
                select(SessionORM.tool_exposure_state).where(SessionORM.id == session_id)
            )
        ).scalar_one()
    assert source_internal == 0
    assert source_state["agents"] == {}
