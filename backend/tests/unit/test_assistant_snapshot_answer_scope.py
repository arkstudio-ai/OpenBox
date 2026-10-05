"""Explicit unread snapshots omit read answers without certifying their sources."""
import asyncio
from contextlib import asynccontextmanager, contextmanager
from contextvars import Context
from datetime import datetime, timezone
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import event, func, select

from assistant import events, snapshot
from assistant.service import ensure_main_session
from db.base import get_db_session, get_engine
from db.models.agent_inbox import AgentInboxItem
from db.models.assistant import AssistantReadCursor
from db.models.message import Message
from db.models.part import Part
from db.models.session import Session
from session.agent_event_log import append_agent_event_locked
from session.internal_parts import _lock_fenced, begin_session_write
from tests.unit.test_assistant_api import client_for, complete_answer, signing_key  # noqa: F401
from tests.unit.test_assistant_foundation import accounts, assistant_database  # noqa: F401


@pytest.fixture(autouse=True)
def offline_and_stable_tokens(monkeypatch):
    from tests.offline_wuying import install_wuying_offline_guard
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "true")
    install_wuying_offline_guard(monkeypatch)
    now = snapshot.time.time()
    # Keep the real token signers and their verification. Only each module's
    # issuance clock is fixed so equality cannot flake across an integer second.
    monkeypatch.setattr(snapshot, "time", SimpleNamespace(time=lambda: now))
    monkeypatch.setattr(events, "time", SimpleNamespace(time=lambda: now))


@contextmanager
def statements():
    values = []

    def record(_connection, _cursor, statement, *_args):
        values.append(" ".join(statement.lower().split()))

    engine = get_engine().sync_engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        yield values
    finally:
        event.remove(engine, "before_cursor_execute", record)


def expected_unread(full):
    return {**full, "answers": [answer for answer in full["answers"]
                                if answer["sequence"] > full["last_seen_sequence"]]}


def content(value):
    # Independent requests check at different DB transaction times. Preserve
    # every other field, including real signed tokens, for full equality.
    checked = datetime.fromisoformat(value["source_checked_at"])
    assert checked.utcoffset().total_seconds() == 0
    assert checked.isoformat(timespec="microseconds") == value["source_checked_at"]
    return {key: item for key, item in value.items() if key != "source_checked_at"}


async def test_scope_http_defaults_remain_full_and_invalid_scope_does_not_create(monkeypatch):
    owner, _, workspace = await accounts()
    async with client_for(owner, workspace, monkeypatch) as client:
        with statements() as sql:
            default = await client.get("/api/assistant")
            explicit = await client.get("/api/assistant", params={"answer_scope": "all"})
            unread = await client.get("/api/assistant", params={"answer_scope": "unread"})
            invalid = await client.get("/api/assistant", params={"answer_scope": "available"})
        assert default.status_code == explicit.status_code == unread.status_code == 200
        assert default.json() == explicit.json() == unread.json()
        assert default.json()["state"] == "not_created" and invalid.status_code == 422
        assert default.json()["source_checked_at"] is None
        assert not any(statement.startswith(("insert", "update", "delete")) for statement in sql)
    with pytest.raises(ValueError):
        await snapshot.get_snapshot(user_id=owner, workspace_id=workspace, answer_scope="available")
    async with get_db_session() as db:
        for model in (Session, AgentInboxItem, AssistantReadCursor):
            assert await db.scalar(select(func.count()).select_from(model).where(model.user_id == owner)) == 0


async def test_all_read_snapshot_omits_answers_and_does_zero_answer_source_checks(monkeypatch, record_property):
    owner, _, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace, model="test/model")
    await complete_answer(owner, workspace, main, "scope-older")
    await complete_answer(owner, workspace, main, "scope-newer")
    first = await snapshot.get_snapshot(user_id=owner, workspace_id=workspace)
    newest = first["answers"][0]
    await snapshot.advance_read_cursor(user_id=owner, workspace_id=workspace, main_id=main.id,
        last_seen_sequence=newest["sequence"], display_token=newest["display_token"])
    digests, sources = [], []
    digest, validate = snapshot._answer_digest, snapshot.validate_message_sources

    async def count_digest(db, message, **kwargs):
        digests.append(message.id)
        return await digest(db, message, **kwargs)

    async def count_source(db, message, **kwargs):
        sources.append(message.id)
        return await validate(db, message, **kwargs)

    monkeypatch.setattr(snapshot, "_answer_digest", count_digest)
    monkeypatch.setattr(snapshot, "validate_message_sources", count_source)
    async with client_for(owner, workspace, monkeypatch) as client:
        with statements() as full_sql:
            response = await client.get("/api/assistant")
        assert response.status_code == 200
        full = response.json()
        assert len(digests) == len(sources) == len(full["answers"]) == 2
        assert content((await client.get("/api/assistant", params={"answer_scope": "all"})).json()) == content(full)
        digests.clear()
        sources.clear()
        with statements() as unread_sql:
            response = await client.get("/api/assistant", params={"answer_scope": "unread"})
        assert response.status_code == 200
        assert content(response.json()) == content(expected_unread(full))
        assert response.json()["answers"] == []
        assert digests == sources == []
        assert not any("from parts " in statement or "join parts " in statement for statement in unread_sql)
        assert not any(statement.startswith(("insert", "update", "delete")) for statement in unread_sql)
        assert sum(statement.startswith("select") for statement in unread_sql) < sum(
            statement.startswith("select") for statement in full_sql)
    record_property("all_read_sql", json.dumps({"full_selects": sum(s.startswith("select") for s in full_sql),
        "unread_selects": sum(s.startswith("select") for s in unread_sql),
        "answer_digest_calls": len(digests), "answer_source_checks": len(sources), "part_reads": 0}))


async def test_mixed_snapshot_keeps_tasks_and_issues_real_unread_token_then_sees_new_answer(monkeypatch):
    owner, _, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace, model="test/model")
    await complete_answer(owner, workspace, main, "scope-first")
    await complete_answer(owner, workspace, main, "scope-second")
    initial = await snapshot.get_snapshot(user_id=owner, workspace_id=workspace)
    older = initial["answers"][-1]
    await snapshot.advance_read_cursor(user_id=owner, workspace_id=workspace, main_id=main.id,
        last_seen_sequence=older["sequence"], display_token=older["display_token"])
    async with client_for(owner, workspace, monkeypatch) as client:
        accepted = await client.post("/api/assistant/tasks", json={
            "idempotency_key": "unread-scope-task", "project_id": main.project_id, "title": "Read-only scope fixture",
            "input": {"text": "Keep this fixture task queued.", "delivery": "followup"}})
        assert accepted.status_code == 202
        full = (await client.get("/api/assistant")).json()
        assert len(full["tasks"]) == 1
        checked, original = [], snapshot._answer_digest

        async def observe(db, message, **kwargs):
            checked.append(message.id)
            return await original(db, message, **kwargs)

        monkeypatch.setattr(snapshot, "_answer_digest", observe)
        unread = (await client.get("/api/assistant", params={"answer_scope": "unread"})).json()
        assert content(unread) == content(expected_unread(full))
        assert checked == [full["answers"][0]["message_id"]]
        answer = unread["answers"][0]
        advanced = await client.post("/api/assistant/read-cursor", json={
            "last_seen_sequence": answer["sequence"], "display_token": answer["display_token"]})
        assert advanced.status_code == 200 and advanced.json()["last_seen_sequence"] == answer["sequence"]
        _, new_message, _ = await complete_answer(owner, workspace, main, "scope-third")
        checked.clear()
        latest = (await client.get("/api/assistant", params={"answer_scope": "unread"})).json()
        assert checked == [new_message.id]
        assert len(latest["answers"]) == latest["unread_count"] == 1
        assert latest["answers"][0]["message_id"] == new_message.id
        assert latest["answers"][0]["available"] and latest["answers"][0]["display_token"]
        assert latest["last_seen_sequence"] == answer["sequence"]


@pytest.mark.parametrize("changed_part", ["source", "answer"])
async def test_unread_answers_and_display_tokens_still_revalidate_current_sources(monkeypatch, changed_part):
    owner, _, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace, model="test/model")
    _, message, part = await complete_answer(owner, workspace, main, "scope-source")
    async with client_for(owner, workspace, monkeypatch) as client:
        first = (await client.get("/api/assistant", params={"answer_scope": "unread"})).json()
        shown = first["answers"][0]
        assert shown["available"] and first["unread_count"] == 1
        async with get_db_session() as db:
            row = await db.get(Part, part.id) if changed_part == "answer" else await db.scalar(
                select(Part).where(Part.message_id == message.parent_id, Part.type == "text"))
            row.data = {**row.data, "text": "" if changed_part == "answer" else "Changed original source"}
        current = (await client.get("/api/assistant", params={"answer_scope": "unread"})).json()
        full = (await client.get("/api/assistant")).json()
        assert content(current) == content(full)
        assert current["answers"] == [{"message_id": message.id, "sequence": shown["sequence"], "available": False}]
        assert current["unread_count"] == 0
        rejected = await client.post("/api/assistant/read-cursor", json={
            "last_seen_sequence": shown["sequence"], "display_token": shown["display_token"]})
        assert rejected.status_code in {409, 410}
    async with get_db_session() as db:
        assert await db.get(AssistantReadCursor, (main.id, owner)) is None


async def test_original_51_candidate_window_drives_bounds_even_when_filtered_answers_are_empty(monkeypatch):
    owner, _, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace, model="test/model")
    now, identities, sequences = datetime.now(timezone.utc), [], []
    # These unavailable metadata rows test the real candidate window; they
    # are not fabricated successful source proofs. Real answers are above.
    async with get_db_session() as db:
        await begin_session_write(db)
        row = await _lock_fenced(db, main.id, owner)
        for index in range(51):
            message_id = uuid4().hex
            identities.append(message_id)
            db.add(Message(id=message_id, session_id=main.id, user_id=owner, role="assistant", finish="stop",
                summary=False, error={"name": "UnavailableFixture", "message": str(index)}, created_at=now))
            terminal = await append_agent_event_locked(db, row, kind="turn.finished", message_id=message_id,
                payload={"finish": "stop", "test": "unread answer window"})
            sequences.append(terminal.sequence)
        await append_agent_event_locked(db, row, kind="turn.finished", message_id=identities[0], payload={"recovery": True})
        summary_id = uuid4().hex
        db.add(Message(id=summary_id, session_id=main.id, user_id=owner, role="assistant", finish="stop",
            summary=True, created_at=now))
        await append_agent_event_locked(db, row, kind="turn.finished", message_id=summary_id, payload={"summary": True})
    async with client_for(owner, workspace, monkeypatch) as client:
        for seen, lower_bound in ((0, True), (sequences[0], True), (sequences[1], False), (sequences[-1], False)):
            async with get_db_session() as db:
                cursor = await db.get(AssistantReadCursor, (main.id, owner))
                if cursor is None:
                    db.add(AssistantReadCursor(assistant_session_id=main.id, user_id=owner,
                        last_seen_sequence=seen, updated_at=now))
                else:
                    cursor.last_seen_sequence = seen
            full = (await client.get("/api/assistant")).json()
            unread = (await client.get("/api/assistant", params={"answer_scope": "unread"})).json()
            assert len(full["answers"]) == 50
            assert [answer["message_id"] for answer in full["answers"]] == list(reversed(identities[1:]))
            assert content(unread) == content(expected_unread(full))
            assert unread["next_before_sequence"] == sequences[1]
            assert unread["unread_count_is_lower_bound"] is lower_bound
            if seen == sequences[-1]:
                assert unread["answers"] == []
            previous = (await client.get("/api/assistant", params={
                "before_sequence": unread["next_before_sequence"], "limit": 1})).json()
            previous_unread = (await client.get("/api/assistant", params={
                "before_sequence": unread["next_before_sequence"], "limit": 1, "answer_scope": "unread"})).json()
            assert content(previous_unread) == content(expected_unread(previous))
            assert previous["answers"][0]["message_id"] == identities[0]
            assert previous_unread["next_before_sequence"] is None


@pytest.mark.parametrize("change", ["source", "read_cursor"])
async def test_unread_window_and_durable_seen_share_one_rr_but_next_request_is_fresh(monkeypatch, change):
    if get_engine().dialect.name != "postgresql":
        pytest.skip("Independent writer consistency requires PostgreSQL MVCC")
    owner, _, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace, model="test/model")
    _, message, _ = await complete_answer(owner, workspace, main, "scope-race")
    full = await snapshot.get_snapshot(user_id=owner, workspace_id=workspace)
    shown = full["answers"][0]
    original, changed = snapshot._answer_candidates, False

    async def race(db, **kwargs):
        nonlocal changed
        window = await original(db, **kwargs)
        if not changed:
            changed = True
            if change == "read_cursor":
                # A concurrent HTTP request has its own context. Running its
                # source validator inside this snapshot's ContextVar walk
                # would deliberately invalidate the reader as foreign DB use.
                await asyncio.create_task(snapshot.advance_read_cursor(
                    user_id=owner, workspace_id=workspace, main_id=main.id,
                    last_seen_sequence=shown["sequence"], display_token=shown["display_token"]), context=Context())
            else:
                async with get_db_session() as writer:
                    part = await writer.scalar(select(Part).where(Part.message_id == message.parent_id, Part.type == "text"))
                    part.data = {**part.data, "text": "Independent writer after candidate read"}
        return window

    monkeypatch.setattr(snapshot, "_answer_candidates", race)
    async with client_for(owner, workspace, monkeypatch) as client:
        before = (await client.get("/api/assistant", params={"answer_scope": "unread"})).json()
        assert content(before) == content(full) and before["unread_count"] == 1
        after = (await client.get("/api/assistant", params={"answer_scope": "unread"})).json()
        assert after["unread_count"] == 0
        if change == "read_cursor":
            assert after["last_seen_sequence"] == shown["sequence"] and after["answers"] == []
        else:
            assert after["last_seen_sequence"] == 0
            assert after["answers"] == [{"message_id": message.id, "sequence": shown["sequence"], "available": False}]


@pytest.mark.parametrize("answer_scope", ["all", "unread"])
async def test_snapshot_check_time_is_post_validation_postgres_clock_barrier(monkeypatch, answer_scope):
    if get_engine().dialect.name != "postgresql":
        pytest.skip("PostgreSQL validation-completion clock contract")
    owner, _, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace, model="test/model")
    await complete_answer(owner, workspace, main, "scope-db-clock")
    original, observations = snapshot._answer_candidates, []
    original_snapshot = snapshot.source_snapshot

    @asynccontextmanager
    async def observe_snapshot(**kwargs):
        async with original_snapshot(**kwargs) as (db, checks):
            entry = {"clocks": []}
            observations.append(entry)
            scalar = db.scalar

            async def observe_scalar(statement, *args, **options):
                value = await scalar(statement, *args, **options)
                if "clock_timestamp()" in str(statement):
                    entry["clocks"].append(value.astimezone(timezone.utc).isoformat(timespec="microseconds"))
                return value

            monkeypatch.setattr(db, "scalar", observe_scalar)
            yield db, checks

    async def observe_time(db, **kwargs):
        observations[-1]["transaction_start"] = ((await db.scalar(select(func.current_timestamp())))
            .astimezone(timezone.utc).isoformat(timespec="microseconds"))
        await db.scalar(select(func.clock_timestamp()))
        return await original(db, **kwargs)

    def forbidden_python_clock(*_args, **_kwargs):
        pytest.fail("PostgreSQL source_checked_at must come from the source transaction, not Python")

    monkeypatch.setattr(snapshot, "_answer_candidates", observe_time)
    monkeypatch.setattr(snapshot, "source_snapshot", observe_snapshot)
    monkeypatch.setattr(snapshot, "datetime", SimpleNamespace(now=forbidden_python_clock))
    async with client_for(owner, workspace, monkeypatch) as client:
        first = (await client.get("/api/assistant", params={"answer_scope": answer_scope})).json()
        second = (await client.get("/api/assistant", params={"answer_scope": answer_scope})).json()
    assert content(first) == content(second)
    for response, observed in zip((first, second), observations, strict=True):
        prior, completed = observed["clocks"]
        assert completed > prior >= observed["transaction_start"]
        assert response["source_checked_at"] == completed
    assert second["source_checked_at"] > first["source_checked_at"]
