"""Final native ASR is human evidence, not an assistant execution outcome."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select

from api.voice import CallEnded, _provider_event
from assistant import memory as assistant_memory
from db.base import get_db_session
from db.models.agent_event import AgentEvent
from db.models.agent_inbox import AgentInboxItem
from db.models.memory import UserMemory
from db.models.memory_pipeline import MemoryExtractionJob, MemoryTurnCompletion
from db.models.memory_protocol import MemoryWriteReceipt
from db.models.memory_v2 import MemorySource
from db.models.message import Message
from db.models.session import Session
from db.models.voice import VoiceCall
from db.models.workspace import WorkspaceMember
from memory import jobs, service, settings
from memory.extraction import MemoryExtractionWorker
from memory.policy import resolve_access_scope
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_memory_extraction import run_turn, setup
from tests.unit.test_automatic_knowledge import Verifier
from voice.assistant_link import AssistantLink, VoiceTurnRef
from voice import memory_capture as capture
from voice.provider import ProviderEvent
from voice.tools import CallScope

SAID = "我的店叫晚风茶馆。"


async def seed(monkeypatch):
    owner, workspace, main, project, config = await setup(monkeypatch)
    call_id = uuid4().hex
    async with get_db_session() as db:
        db.add(VoiceCall(id=call_id, user_id=owner, workspace_id=workspace, main_session_id=main.id,
            client="web", model="test/native", voice="test", status="active",
            started_at=datetime.now(timezone.utc), duration_seconds=0, turns=0, usage={},
            estimated_yuan="0", unreported_rounds=0, price_date="2026-10-22"))
    scope = CallScope(user_id=owner, workspace_id=workspace, main_session_id=main.id,
                      call_id=call_id, lang="zh")
    return scope, project, config


async def test_batched_native_bindings_keep_exact_receipts_and_recheck_membership(monkeypatch):
    scope, _, _ = await seed(monkeypatch)
    identities = [await capture.capture_final(scope, text=SAID, item_id=f"batch-{i}", provider_session=0)
                  for i in range(2)]
    async with get_db_session() as db:
        access = await resolve_access_scope(db, user_id=scope.user_id, workspace_id=scope.workspace_id)
        sources = list(await db.scalars(select(MemorySource).where(MemorySource.id.in_(identities))))
        bindings = await capture.prefetch_bindings(db, access, sources)
        assert all([await capture.source_available(db, access, source, binding=bindings[source.id])
                    for source in sources])
        await db.execute(delete(MemoryTurnCompletion).where(
            MemoryTurnCompletion.id == capture._receipt_id(identities[0])))
        bindings = await capture.prefetch_bindings(db, access, sources)
        assert {source.id: await capture.source_available(db, access, source, binding=bindings[source.id])
                for source in sources} == {identities[0]: False, identities[1]: True}
        (await db.get(WorkspaceMember, (scope.workspace_id, scope.user_id))).status = "inactive"
        await db.flush()
        bindings = await capture.prefetch_bindings(db, access, sources)
        assert all(binding == (None, None, None) for binding in bindings.values())


def extractor(seen):
    def extract(frozen):
        seen.extend(frozen.sources)
        return {"candidates": [{"type": "USER_PROFILE", "summary": "用户的店叫晚风茶馆。",
            "fact_key": "personal.business.name", "confidence": 90, "source_indexes": [0],
            "quotes": [{"source_index": 0, "quote": SAID}]}]}
    return extract


def link_for(scope):
    return AssistantLink(call_id=scope.call_id, user_id=scope.user_id, workspace_id=scope.workspace_id,
                         main_session_id=scope.main_session_id, lang="zh", turn_timeout=1)


async def forward(scope, link, *, text=SAID, item_id="asr-1"):
    ref = VoiceTurnRef(id=uuid4().hex, provider_call_id=uuid4().hex, text=text, transcript=text,
                      requested=1, context={"spoken_input_id": item_id})
    await link.start(ref)
    return ref


async def test_native_speech_is_durable_before_bridge_and_becomes_readable_memory(monkeypatch):
    scope, project, _config = await seed(monkeypatch)
    link = link_for(scope)
    heard = []

    async def on_event(event):
        async with get_db_session() as db:
            sources = list((await db.scalars(select(MemorySource))).all())
            assert [row.body for row in sources] == [SAID]
            assert await db.scalar(select(func.count()).select_from(Message)) == 0
            assert await db.scalar(select(func.count()).select_from(AgentInboxItem)) == 0
            assert await db.scalar(select(func.count()).select_from(AgentEvent)) == 0
        heard.append(event.text)

    bridge = SimpleNamespace(scope=scope, call_id=scope.call_id, session=0, on_provider_event=on_event)
    await _provider_event(bridge, link, ProviderEvent("user_transcript", text=SAID, item_id="asr-1"))
    assert heard == [SAID]
    async with get_db_session() as db:
        receipt = await db.scalar(select(MemoryTurnCompletion))
        assert receipt.run_id == receipt.result_message_id == "" and receipt.run_generation == 0
        assert "body" not in receipt.source_boundaries[0]
        assert receipt.source_boundaries[0]["message_id"] is None
    seen = []
    assert await MemoryExtractionWorker(extractor=extractor(seen), verifier=Verifier()).run_once() == "SUCCEEDED"
    assert [source["body"] for source in seen] == [SAID]
    async with get_db_session() as db:
        row = await db.scalar(select(UserMemory))
        assert row.status == "ACTIVE" and row.project_id is None
        access = await resolve_access_scope(db, user_id=scope.user_id, workspace_id=scope.workspace_id)
        item, bodies = await assistant_memory._current(db, access, scope.main_session_id, row, {})
        assert SAID in bodies.values()
        assert item["sources"][0]["origin"] == "voice_call"
        source = await db.scalar(select(MemorySource))
        exact = await service.read_source_in_scope(db, access=access, source_id=source.id)
        assert exact["available"] and exact["body"] == SAID
        narrow = await resolve_access_scope(db, user_id=scope.user_id, workspace_id=scope.workspace_id,
                                            project_id=project)
        assert not await service.source_body_is_available(db, narrow, source)
        memory_id = row.id
    shown = await service.get_sources(user_id=scope.user_id, workspace_id=scope.workspace_id, memory_id=memory_id)
    assert shown[0]["body_available"] and shown[0]["body"] == SAID


async def test_final_identity_is_idempotent_and_rotation_is_distinct(monkeypatch):
    scope, _, _ = await seed(monkeypatch)
    kwargs = dict(text="  " + SAID + "\n", item_id="asr-1", provider_session=0)
    first, second = await asyncio.gather(capture.capture_final(scope, **kwargs), capture.capture_final(scope, **kwargs))
    assert first == second
    with pytest.raises(jobs.ExtractionSourceInvalid, match="revision_changed"):
        await capture.capture_final(scope, **{**kwargs, "text": "我的店叫别的名字。"})
    later = await capture.capture_final(scope, **{**kwargs, "provider_session": 1})
    assert later != first
    async with get_db_session() as db:
        rows = list((await db.scalars(select(MemorySource))).all())
        assert len(rows) == 2 and all(row.body == kwargs["text"] for row in rows)
        assert await db.scalar(select(func.count()).select_from(MemoryExtractionJob)) == 2


@pytest.mark.parametrize("pause", ["account", "session", "write_disabled", "extract_disabled"])
async def test_paused_or_disabled_capture_never_backfills_even_if_job_lost(monkeypatch, pause):
    scope, _, config = await seed(monkeypatch)
    if pause == "account":
        await settings.update_settings(scope.user_id, auto_save=False)
    elif pause == "session":
        await settings.update_settings(scope.user_id, session_id=scope.main_session_id, session_paused=True)
    elif pause == "write_disabled":
        config.v2_write = False
    else:
        config.auto_extract = False
    identity = await capture.capture_final(scope, text=SAID, item_id="asr-1", provider_session=0)
    async with get_db_session() as db:
        job = await db.scalar(select(MemoryExtractionJob))
        assert job.state == "CANCELLED"
        assert (await db.get(MemorySource, identity)).body == SAID
        # Recovery must use the frozen saving decision, even after a lost job.
        await db.execute(delete(MemoryExtractionJob))
    await settings.update_settings(scope.user_id, auto_save=True, session_id=scope.main_session_id, session_paused=False)
    config.v2_write = config.auto_extract = True
    assert await jobs.recover_extraction_jobs(include_inbox=False) == 1
    assert await jobs.claim_job("native-test") is None
    async with get_db_session() as db:
        assert (await db.scalar(select(MemoryExtractionJob))).state == "CANCELLED"


@pytest.mark.parametrize("change", ["call_owner", "call_workspace", "call_session", "call_delete",
                                     "source_body", "source_metadata", "source_occurrence", "session_delete",
                                     "session_parent", "session_public", "member_revoked"])
async def test_source_authority_changes_block_extraction_before_model(monkeypatch, change):
    scope, _, _ = await seed(monkeypatch)
    identity = await capture.capture_final(scope, text=SAID, item_id="asr-1", provider_session=0)
    async with get_db_session() as db:
        call, source = await db.get(VoiceCall, scope.call_id), await db.get(MemorySource, identity)
        session = await db.get(Session, scope.main_session_id)
        if change == "call_delete":
            await db.delete(call)
        elif change.startswith("call_"):
            setattr(call, {"call_owner": "user_id", "call_workspace": "workspace_id", "call_session": "main_session_id"}[change], "wrong")
        elif change == "source_body":
            source.body = "被修改的原话"
        elif change == "source_metadata":
            source.source_metadata = {**source.source_metadata, "item_id": "another-item"}
        elif change == "source_occurrence":
            source.occurred_at = None
        elif change == "session_delete":
            session.is_deleted = True
        elif change == "session_parent":
            session.parent_id = "another-session"
        elif change == "session_public":
            session.visibility = "workspace"
        else:
            (await db.get(WorkspaceMember, (scope.workspace_id, scope.user_id))).status = "inactive"
    seen = []
    result = await MemoryExtractionWorker(extractor=extractor(seen), verifier=Verifier()).run_once()
    assert result in {None, "CANCELLED"} and not seen


async def test_call_ending_does_not_revoke_already_confirmed_human_words(monkeypatch):
    scope, _, _ = await seed(monkeypatch)
    await capture.capture_final(scope, text=SAID, item_id="asr-1", provider_session=0)
    async with get_db_session() as db:
        call = await db.get(VoiceCall, scope.call_id)
        call.status, call.end_reason, call.ended_at = "ended", "network", datetime.now(timezone.utc)
    assert await MemoryExtractionWorker(extractor=extractor([]), verifier=Verifier()).run_once() == "SUCCEEDED"


@pytest.mark.parametrize("forget", [False, True])
async def test_forward_reuses_exact_capture_and_cannot_relearn_deleted_source(monkeypatch, forget):
    scope, _, _ = await seed(monkeypatch)
    wakes = []
    monkeypatch.setattr("agent.inbox.schedule_inbox_wake", lambda *args: wakes.append(args))
    link = link_for(scope)
    identity = await capture.capture_final(scope, text=SAID, item_id="asr-1", provider_session=0)
    link.bind_native_source("asr-1", SAID, identity)
    assert not wakes
    seen = []
    worker = MemoryExtractionWorker(extractor=extractor(seen), verifier=Verifier())
    assert await worker.run_once() == "SUCCEEDED"
    if forget:
        async with get_db_session() as db:
            row = await db.scalar(select(UserMemory))
        await service.forget_memory(user_id=scope.user_id, workspace_id=scope.workspace_id,
            memory_id=row.id, expected_revision=row.revision, mode="sources", source_ids=[identity])
    ref = await forward(scope, link)
    receipt = await run_turn(scope.user_id, scope.main_session_id)
    assert receipt.id == ref.inbox_id
    assert len(wakes) == 1
    async with get_db_session() as db:
        item = await db.get(AgentInboxItem, ref.inbox_id)
        assert item.origin_ref["native_voice_source_id"] == identity
        completed = await db.scalar(select(MemoryTurnCompletion).where(MemoryTurnCompletion.result_message_id != ""))
        assert completed.source_boundaries == []
        assert await db.scalar(select(func.count()).select_from(AgentInboxItem)) == 1
    assert await worker.run_once() == "SUCCEEDED"
    assert len(seen) == 1
    assert await capture.capture_final(scope, text=SAID, item_id="asr-1", provider_session=0) == identity
    async with get_db_session() as db:
        source = await db.get(MemorySource, identity)
        assert source.body == (None if forget else SAID)


async def test_unbound_or_mismatched_forward_cannot_supply_a_source_id(monkeypatch):
    scope, _, _ = await seed(monkeypatch)
    monkeypatch.setattr("agent.inbox.schedule_inbox_wake", lambda *_: None)
    link = link_for(scope)
    identity = await capture.capture_final(scope, text=SAID, item_id="asr-1", provider_session=0)
    link.bind_native_source("asr-1", "不同的人类原话", identity)
    ref = VoiceTurnRef(id=uuid4().hex, provider_call_id=uuid4().hex, text=SAID, transcript=SAID,
        requested=1, context={"spoken_input_id": "asr-1", "native_voice_source_id": identity})
    await link.start(ref)
    async with get_db_session() as db:
        item = await db.get(AgentInboxItem, ref.inbox_id)
        assert "native_voice_source_id" not in item.origin_ref


async def test_native_kind_without_capture_receipt_is_never_trusted(monkeypatch):
    scope, _, _ = await seed(monkeypatch)
    identity = await capture.capture_final(scope, text=SAID, item_id="asr-1", provider_session=0)
    async with get_db_session() as db:
        source = await db.get(MemorySource, identity)
        await db.execute(delete(MemoryExtractionJob))
        await db.execute(delete(MemoryTurnCompletion))
        access = await resolve_access_scope(db, user_id=scope.user_id, workspace_id=scope.workspace_id)
        assert not await service.source_is_available(db, access, source)


async def test_hangup_drains_received_final_asr_without_forwarding_to_bridge(monkeypatch):
    scope, _, _ = await seed(monkeypatch)
    started, release = asyncio.Event(), asyncio.Event()
    original = capture.capture_final
    async def blocked(*args, **kwargs):
        started.set()
        await release.wait()
        return await original(*args, **kwargs)
    monkeypatch.setattr(capture, "capture_final", blocked)
    heard = []
    async def on_event(event):
        heard.append(event)
    bridge = SimpleNamespace(scope=scope, call_id=scope.call_id, session=0, on_provider_event=on_event)
    pending = asyncio.create_task(_provider_event(bridge, link_for(scope),
        ProviderEvent("user_transcript", text=SAID, item_id="asr-1")))
    await started.wait()
    pending.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    async with get_db_session() as db:
        assert (await db.scalar(select(MemorySource))).body == SAID
        assert await db.scalar(select(func.count()).select_from(AgentInboxItem)) == 0
    assert heard == []


@pytest.mark.parametrize("failure", ["missing_identity", "write_failure"])
async def test_capture_failure_never_claims_saved_or_runs_recall(monkeypatch, failure):
    scope, _, _ = await seed(monkeypatch)
    if failure == "write_failure":
        async def broken(*_args, **_kwargs):
            raise RuntimeError("SQL unavailable")
        monkeypatch.setattr(capture, "capture_final", broken)
    heard, skipped = [], []
    async def on_event(event):
        heard.append(event)
    bridge = SimpleNamespace(scope=scope, call_id=scope.call_id, session=0, on_provider_event=on_event,
                             skip_recall=skipped.append)
    link = link_for(scope)
    item_id = "" if failure == "missing_identity" else "asr-1"
    # The call goes on and the words are still answered, but never recalled
    # from (the bridge answers them without memory) and never bound as saved.
    await _provider_event(bridge, link, ProviderEvent("user_transcript", text=SAID, item_id=item_id))
    assert [event.text for event in heard] == [SAID] and skipped == [item_id]
    assert not link._native_sources


async def test_partial_asr_and_assistant_output_never_create_human_evidence(monkeypatch):
    scope, _, _ = await seed(monkeypatch)
    heard = []
    async def on_event(event):
        heard.append(event.kind)
    bridge = SimpleNamespace(scope=scope, call_id=scope.call_id, session=0, on_provider_event=on_event)
    for kind in ("user_transcribing", "assistant_transcript", "response_done"):
        await _provider_event(bridge, link_for(scope), ProviderEvent(kind, text=SAID, item_id="asr-1"))
    async with get_db_session() as db:
        assert await db.scalar(select(func.count()).select_from(MemorySource)) == 0
    assert len(heard) == 3


async def test_late_final_preserves_speech_order_and_does_not_overwrite_newer_words(monkeypatch):
    scope, _, _ = await seed(monkeypatch)
    times = iter(datetime.now(timezone.utc) + timedelta(seconds=value) for value in (0, 1, 2, 3))
    clock = capture.OccurrenceClock(now=lambda: next(times))
    heard = []
    async def on_event(event):
        heard.append(event)
    bridge = SimpleNamespace(scope=scope, call_id=scope.call_id, session=0, on_provider_event=on_event)
    link = link_for(scope)
    for event in (ProviderEvent("user_started", item_id="older"),
                  ProviderEvent("user_started", item_id="newer"),
                  ProviderEvent("user_transcript", item_id="newer", text="第二句"),
                  ProviderEvent("user_transcript", item_id="older", text="第一句")):
        await _provider_event(bridge, link, event, occurrence_clock=clock)
    async with get_db_session() as db:
        rows = list((await db.scalars(select(MemorySource).order_by(MemorySource.start_seq))).all())
        assert [row.body for row in rows] == ["第二句", "第一句"]
        assert rows[1].occurred_at < rows[0].occurred_at


async def test_provider_rotating_during_capture_keeps_evidence_without_replaying_old_event(monkeypatch):
    scope, _, _ = await seed(monkeypatch)
    heard = []
    async def on_event(event):
        heard.append(event)
    bridge = SimpleNamespace(scope=scope, call_id=scope.call_id, session=0,
                             provider=object(), on_provider_event=on_event)
    original = capture.capture_final
    async def rotate(*args, **kwargs):
        identity = await original(*args, **kwargs)
        bridge.provider, bridge.session = object(), 1
        return identity
    monkeypatch.setattr(capture, "capture_final", rotate)
    link = link_for(scope)
    await _provider_event(bridge, link, ProviderEvent("user_transcript", text=SAID, item_id="old"))
    async with get_db_session() as db:
        assert (await db.scalar(select(MemorySource))).body == SAID
    assert not heard and not link._native_sources


async def test_revocation_during_grounding_cannot_publish_native_memory(monkeypatch):
    scope, _, _ = await seed(monkeypatch)
    await capture.capture_final(scope, text=SAID, item_id="asr-1", provider_session=0)
    async def revoke():
        async with get_db_session() as db:
            (await db.get(VoiceCall, scope.call_id)).main_session_id = "wrong"
    seen = []
    worker = MemoryExtractionWorker(extractor=extractor(seen), verifier=Verifier(before=revoke))
    assert await worker.run_once() == "CANCELLED"
    async with get_db_session() as db:
        assert await db.scalar(select(func.count()).select_from(UserMemory)) == 0
    assert len(seen) == 1


async def test_oversized_final_is_preserved_and_extraction_failure_is_visible(monkeypatch):
    scope, _, _ = await seed(monkeypatch)
    text = SAID + "原话" * 12000
    identity = await capture.capture_final(scope, text=text, item_id="asr-1", provider_session=0)
    seen = []
    assert await MemoryExtractionWorker(extractor=extractor(seen), verifier=Verifier()).run_once() == "DEAD"
    assert not seen
    status = await jobs.processing_status(user_id=scope.user_id, workspace_id=scope.workspace_id)
    assert len(status["failed"]) == 1 and status["failed"][0]["excerpt"].startswith(SAID)
    async with get_db_session() as db:
        assert (await db.get(MemorySource, identity)).body == text


async def test_native_voice_preference_uses_checked_source_and_records_original_identity(monkeypatch):
    scope, _, _ = await seed(monkeypatch)
    text = "以后打电话时请详细解释。"
    identity = await capture.capture_final(scope, text=text, item_id="asr-1", provider_session=0)
    def extract(frozen):
        assert frozen.sources[0]["id"] == identity
        return {"candidates": [{"type": "PREFERENCE", "summary": "用户希望电话中详细解释。",
            "fact_key": "personal.style.call_detail", "confidence": 90, "source_indexes": [0],
            "quotes": [{"source_index": 0, "quote": text}], "setting": "detailed"}]}
    assert await MemoryExtractionWorker(extractor=extract, verifier=Verifier()).run_once() == "SUCCEEDED"
    async with get_db_session() as db:
        receipt = await db.scalar(select(MemoryWriteReceipt))
        assert receipt.state == "saved" and receipt.result["fields"] == {"call_detail": "detailed"}
        assert receipt.source_refs[0]["source_id"] == identity
        assert receipt.source_refs[0]["source_kind"] == capture.SOURCE_KIND
        assert receipt.source_refs[0]["session_id"] == scope.main_session_id


async def test_forward_after_resuming_cannot_extract_words_captured_while_paused(monkeypatch):
    scope, _, _ = await seed(monkeypatch)
    monkeypatch.setattr("agent.inbox.schedule_inbox_wake", lambda *_: None)
    await settings.update_settings(scope.user_id, auto_save=False)
    identity = await capture.capture_final(scope, text=SAID, item_id="asr-1", provider_session=0)
    link = link_for(scope)
    link.bind_native_source("asr-1", SAID, identity)
    await settings.update_settings(scope.user_id, auto_save=True)
    await forward(scope, link)
    await run_turn(scope.user_id, scope.main_session_id)
    seen = []
    worker = MemoryExtractionWorker(extractor=extractor(seen), verifier=Verifier())
    assert await worker.run_once() == "SUCCEEDED"  # Actual forwarded task; no new human evidence.
    assert await worker.run_once() is None and not seen
    async with get_db_session() as db:
        assert set((await db.scalars(select(MemoryExtractionJob.state))).all()) == {"CANCELLED", "SUCCEEDED"}
