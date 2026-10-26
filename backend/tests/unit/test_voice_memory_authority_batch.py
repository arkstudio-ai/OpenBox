"""Batch native provenance reads without caching authority across SQL passes."""
import pytest
from sqlalchemy import event, select

from db.base import get_db_session
from db.models.memory_pipeline import MemoryTurnCompletion
from db.models.memory_v2 import MemorySource
from db.models.session import Session
from db.models.voice import VoiceCall
from db.models.workspace import WorkspaceMember
from memory import jobs, service
from memory.policy import resolve_access_scope
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_voice_memory_capture import seed
from voice import memory_capture as capture


async def seed_sources(monkeypatch):
    scope, _, _ = await seed(monkeypatch)
    ids = [await capture.capture_final(scope, text=f"我平常记录第{i}种习惯。",
        item_id=f"native-{i}", provider_session=0) for i in range(6)]
    return scope, ids


async def test_native_source_queries_are_per_batch_not_per_utterance(monkeypatch):
    scope, ids = await seed_sources(monkeypatch)
    async with get_db_session() as db:
        access = await resolve_access_scope(db, user_id=scope.user_id, workspace_id=scope.workspace_id)
        sources = list((await db.scalars(select(MemorySource).where(MemorySource.id.in_(ids)))).all())
        expected = [await capture.source_available(db, access, source) for source in sources]
        statements = []
        def before(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)
        event.listen(db.get_bind(), "before_cursor_execute", before)
        try:
            async with service.source_authority(db, access):
                await service.prefetch_source_facts(db, access, sources)
                actual = [await service.source_is_available(db, access, source) for source in sources]
            assert actual == expected == [True] * 6
            native_reads = [sql for sql in statements if "JOIN voice_calls" in sql]
            assert len(native_reads) == 1
            assert "JOIN memory_turn_completions" in native_reads[0]
            assert "JOIN sessions" in native_reads[0]
            assert sum("UNION ALL" in sql and "memory_source_links" in sql for sql in statements) == 1
        finally:
            event.remove(db.get_bind(), "before_cursor_execute", before)


@pytest.mark.parametrize("change", ["receipt_hash", "receipt_boundary", "call_owner",
                                    "session_public", "member_revoked", "metadata_list", "call_id_list"])
async def test_new_pass_rechecks_changed_native_authority(monkeypatch, change):
    scope, ids = await seed_sources(monkeypatch)
    async with get_db_session() as db:
        access = await resolve_access_scope(db, user_id=scope.user_id, workspace_id=scope.workspace_id)
        sources = list((await db.scalars(select(MemorySource).where(MemorySource.id.in_(ids)))).all())
        async with service.source_authority(db, access):
            await service.prefetch_source_facts(db, access, sources)
            assert all([await service.source_is_available(db, access, source) for source in sources])
    async with get_db_session() as db:
        if change.startswith("receipt"):
            row = await db.get(MemoryTurnCompletion, capture._receipt_id(ids[0]))
            if change == "receipt_hash":
                row.input_hash = "changed"
            else:
                row.source_boundaries = [{**row.source_boundaries[0], "start_seq": 999}]
                row.input_hash = jobs._hash(row.source_boundaries)
        elif change == "call_owner":
            from tests.unit.test_assistant_foundation import accounts
            other, _, _ = await accounts()
            (await db.get(VoiceCall, scope.call_id)).user_id = other
        elif change == "session_public":
            (await db.get(Session, scope.main_session_id)).visibility = "workspace"
        elif change in {"metadata_list", "call_id_list"}:
            source = await db.get(MemorySource, ids[0])
            source.source_metadata = (["invalid"] if change == "metadata_list" else
                {**source.source_metadata, "voice_call_id": []})
        else:
            (await db.get(WorkspaceMember, (scope.workspace_id, scope.user_id))).status = "inactive"
    # Even reuse of an old access descriptor cannot bypass the live private
    # assistant check. No binding survives the previous source_authority pass.
    async with get_db_session() as db:
        source = await db.get(MemorySource, ids[0])
        async with service.source_authority(db, access):
            await service.prefetch_source_facts(db, access, [source])
            assert not await service.source_is_available(db, access, source)
