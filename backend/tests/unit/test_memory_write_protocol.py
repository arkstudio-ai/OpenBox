"""Real SQL: atomic receipts, evidence rechecks, settings races and global style."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from assistant import profile
from assistant.service import ensure_main_session
from core.config import OpenBoxConfig
from db.base import get_db_session
from db.models.memory import UserMemory
from db.models.memory_protocol import MemoryWriteReceipt
from db.models.memory_v2 import MemorySource, MemorySourceLink
from db.models.part import Part
from memory import service
from memory.commands import write_profile_proposal, write_proposal
from memory.preferences import apply_profile
from memory.versions import version
from session.session import create_user_message
from tests.unit.test_assistant_foundation import accounts, assistant_database  # noqa: F401


class Verifier:
    def __init__(self, supported=True, during=None):
        self.supported, self.during, self.calls = supported, during, 0

    async def verify(self, items, **kwargs):
        self.calls += 1
        if self.during:
            await self.during()
        return [self.supported] * len(items), {}


async def setup(monkeypatch, text="以后用英文回答我"):
    owner, other, workspace = await accounts()
    monkeypatch.setattr("core.config.get_config", lambda: OpenBoxConfig(model="test/model",
        memory={"automatic_knowledge": False}))
    monkeypatch.setattr("bus.bus.publish", lambda *args, **kwargs: None)
    main = await ensure_main_session(user_id=owner, workspace_id=workspace, model="test/model")
    message = await create_user_message(main.id, text, user_id=owner, agent="assistant", origin="human",
        origin_ref={"actor_user_id": owner})
    return owner, other, workspace, main, message


async def test_source_bound_memory_is_machine_verified_and_replay_is_idempotent(monkeypatch):
    owner, _, workspace, main, message = await setup(monkeypatch)
    verifier = Verifier()
    args = dict(user_id=owner, workspace_id=workspace, session_id=main.id, message_ids=[message.id],
        operation_id="test:save", summary="用户希望用英文回答", fact_key="personal.style.language", verifier=verifier)
    receipt = await write_proposal(**args)
    assert receipt["state"] == "saved"
    assert await write_proposal(**args) == receipt
    assert verifier.calls == 1
    async with get_db_session() as db:
        row = await db.get(UserMemory, receipt["memory_id"])
        assert row.owner == "SYSTEM_VERIFIED" and row.confirmation_actor_id is None
        sources = (await db.scalars(select(MemorySource).join(MemorySourceLink,
            MemorySourceLink.source_id == MemorySource.id).where(MemorySourceLink.memory_id == row.id))).all()
        assert sources and all(source.body == "以后用英文回答我" and source.message_id == message.id for source in sources)
        assert await db.scalar(select(func.count()).select_from(MemoryWriteReceipt)) == 1
    assert "用户希望用英文回答" in await profile.settings_section(owner, workspace)
    assert "用户希望用英文回答" in await profile.user_section(owner, workspace)


async def test_quote_match_does_not_grant_semantic_support(monkeypatch):
    owner, _, workspace, main, message = await setup(monkeypatch, "客户说以后叫他王总")
    receipt = await write_profile_proposal(user_id=owner, workspace_id=workspace, session_id=main.id,
        message_ids=[message.id], operation_id="test:third-party", patch={"address": "王总"}, verifier=Verifier(False))
    assert receipt["state"] == "needs_context"
    assert (await profile.load(owner)).address == ""


async def test_chat_settings_verify_the_same_atomic_field_as_background_extraction(monkeypatch):
    owner, _, workspace, main, message = await setup(monkeypatch,
        "咱们继续英语练习吧。从现在开始，平时跟我聊天都用英语。")
    class TypedVerifier:
        async def verify(self, items, **kwargs):
            assert len(items) == 1
            assert items[0]['account_setting'] == {'language': 'English'}
            assert 'preferred conversation language' in items[0]['claim']
            assert "assistant's name" not in items[0]['claim']
            assert items[0]['sources'] == ['咱们继续英语练习吧。从现在开始，平时跟我聊天都用英语。']
            return [True], {'grounding_policy_version': 'test', 'intent_counts': {'standing_instruction': 1}}
    result = await write_profile_proposal(user_id=owner, workspace_id=workspace, session_id=main.id,
        message_ids=[message.id], operation_id='test:typed-language', patch={'language': 'English'},
        verifier=TypedVerifier())
    assert result['state'] == 'saved'
    assert result['verification']['intent_counts'] == {'standing_instruction': 1}


async def test_language_is_current_account_state_and_not_a_ranked_memory(monkeypatch):
    from memory.packet import current_settings
    from voice.memory_context import instructions
    from types import SimpleNamespace
    owner, _, workspace, main, message = await setup(monkeypatch)
    receipt = await write_profile_proposal(user_id=owner, workspace_id=workspace, session_id=main.id,
        message_ids=[message.id], operation_id="test:language", patch={"language": "英语"}, verifier=Verifier())
    assert receipt["state"] == "saved"
    async with get_db_session() as db:
        settings = await current_settings(db, owner)
        assert await db.scalar(select(func.count()).select_from(UserMemory).where(UserMemory.user_id == owner)) == 0
    assert settings["language"] == "英语"
    assert "Preferred conversation language: 英语" in await profile.settings_section(owner, workspace)
    voice = instructions(SimpleNamespace(lang="zh-CN"), {"settings": settings})
    assert "交谈语言是「英语」" in voice
    # Clearing a default is a newer decision too; replay cannot resurrect it.
    await profile.save(owner, {"language": ""}, expected_revision=receipt["revision"])
    assert (await profile.load(owner)).language == ""
    async with get_db_session() as db:
        cleared = await current_settings(db, owner)
    assert cleared["language"] == ""
    assert "已清除固定交谈语言" in instructions(SimpleNamespace(lang="zh-CN"), {"settings": cleared})
    assert "cleared their fixed conversation language" in await profile.settings_section(owner, workspace)
    repeated = await write_profile_proposal(user_id=owner, workspace_id=workspace, session_id=main.id,
        message_ids=[message.id], operation_id="test:language", patch={"language": "英语"}, verifier=Verifier())
    assert repeated == receipt and (await profile.load(owner)).language == ""


async def test_newer_ui_setting_wins_while_model_verification_is_in_flight(monkeypatch):
    owner, _, workspace, main, message = await setup(monkeypatch, "以后回答简短些")
    async def newer():
        await profile.save(owner, {"length": "detailed"})
    receipt = await write_profile_proposal(user_id=owner, workspace_id=workspace, session_id=main.id,
        message_ids=[message.id], operation_id="test:late", patch={"length": "brief"}, verifier=Verifier(during=newer))
    assert receipt["state"] == "conflict"
    assert (await profile.load(owner)).length == "detailed"


async def test_tool_and_extractor_of_same_human_setting_commit_one_revision(monkeypatch):
    from memory.commands import human_sources, source_refs
    from memory.preferences import apply_extracted_settings
    from types import SimpleNamespace
    from wiki_compiler.hashing import canonical_hash
    owner, _, workspace, main, message = await setup(monkeypatch)
    saved = await write_profile_proposal(user_id=owner, workspace_id=workspace, session_id=main.id,
        message_ids=[message.id], operation_id='first-writer', patch={'language': '英语'}, verifier=Verifier())
    async with get_db_session() as db:
        sources, _ = await human_sources(db, user_id=owner, workspace_id=workspace,
            session_id=main.id, message_ids=[message.id])
        frozen = SimpleNamespace(user_id=owner, input_hash='source', job_id='second-writer', sources=sources)
        proposal = {'fact_key': 'personal.style.language', 'setting': 'English', 'source_indexes': [0]}
        repeated = await apply_extracted_settings(db, frozen, [proposal],
            {'input_hash': 'source', 'supported': [canonical_hash(proposal)]}, expected_revision=saved['revision'])
        assert repeated['state'] == 'saved' and repeated['replayed'] is True
        assert repeated['revision'] == saved['revision']
        assert repeated['context_version'] == saved['context_version'] == await version(db, owner)
        assert repeated['fields'] == {'language': '英语'}
    # A newer direct decision must still win over the old human source.
    newer = await profile.save(owner, {'language': '中文'})
    async with get_db_session() as db:
        late = await apply_profile(db, user_id=owner, patch={'language': 'English'}, operation_id='late-writer',
            via='chat', occurred_at=message.created_at, source_refs=source_refs(sources), checked_source=True)
        assert late['state'] == 'conflict'


@pytest.mark.parametrize("writer", ["settings", "chat", "intro"])
async def test_profile_writers_preserve_newer_version_fields_without_exposing_them(monkeypatch, writer):
    from db.repository.preference_repo import PgPreferenceRepo
    owner, _, _, _, _ = await setup(monkeypatch)
    future = {"mode": "future-version-value"}
    decision = {"at": datetime.now(timezone.utc).isoformat(), "via": "settings", "version": 2}
    await PgPreferenceRepo().upsert(owner, extra={
        profile.PREFERENCE_KEY: {"language": "英语", "future_preference": future},
        profile.DECIDED_KEY: {"language": decision, "future_preference": decision},
    })
    if writer == "intro":
        await profile.intro_event(owner, "dismiss")
    else:
        await profile.save(owner, {"length": "brief"}, via=writer, checked_source=writer == "chat")
    extra = (await PgPreferenceRepo().get(owner))["extra"]
    assert extra[profile.PREFERENCE_KEY]["future_preference"] == future
    assert extra[profile.DECIDED_KEY]["future_preference"] == decision
    current = await profile.load_view(owner)
    assert current["language"] == "英语"
    assert "future_preference" not in current and "future_preference" not in current["decided"]
    assert "future-version-value" not in await profile.settings_section(owner)


async def test_old_source_cannot_override_new_decision_even_with_current_base_revision(monkeypatch):
    owner, _, _, _, _ = await setup(monkeypatch)
    await profile.save(owner, {"length": "detailed"})
    current = await profile.load_view(owner)
    async with get_db_session() as db:
        await service.lock_memory_authority(db, user_id=owner)
        receipt = await apply_profile(db, user_id=owner, patch={"length": "brief"},
            operation_id="test:old-source", via="chat", checked_source=True,
            expected_revision=current["revision"], occurred_at=datetime.now(timezone.utc) - timedelta(days=1))
    assert receipt["state"] == "conflict"
    assert (await profile.load(owner)).length == "detailed"


async def test_profile_receipt_and_change_clock_rollback_with_authority(monkeypatch):
    owner, _, _, _, _ = await setup(monkeypatch)
    with pytest.raises(RuntimeError):
        async with get_db_session() as db:
            await service.lock_memory_authority(db, user_id=owner)
            receipt = await apply_profile(db, user_id=owner, patch={"address": "林姐"}, operation_id="test:crash")
            assert receipt["state"] == "saved"
            raise RuntimeError("crash before commit")
    assert (await profile.load(owner)).address == ""
    async with get_db_session() as db:
        assert await db.get(MemoryWriteReceipt, (owner, "test:crash")) is None
        assert await version(db, owner) == 0
    await profile.save(owner, {"address": "林姐"}, operation_id="test:crash")
    after = await profile.load_view(owner)
    await profile.save(owner, {"address": "林姐"}, operation_id="test:crash")
    assert await profile.load_view(owner) == after


async def test_changed_original_is_rechecked_after_external_verifier(monkeypatch):
    owner, _, workspace, main, message = await setup(monkeypatch)
    async def edit_source():
        async with get_db_session() as db:
            part = await db.scalar(select(Part).where(Part.message_id == message.id, Part.type == "text"))
            part.data = {**part.data, "text": "不保存这条"}
    with pytest.raises(service.MemoryConflict):
        await write_proposal(user_id=owner, workspace_id=workspace, session_id=main.id, message_ids=[message.id],
            operation_id="test:source-change", summary="用户希望英文回答", verifier=Verifier(during=edit_source))
    async with get_db_session() as db:
        assert await db.scalar(select(func.count()).select_from(UserMemory)) == 0


async def test_direct_edit_cas_and_model_replay_never_restore_an_old_setting(monkeypatch):
    owner, _, workspace, main, message = await setup(monkeypatch, "以后叫我林姐")
    args = dict(user_id=owner, workspace_id=workspace, session_id=main.id, message_ids=[message.id],
        operation_id="test:profile", patch={"address": "林姐"}, verifier=Verifier())
    first = await write_profile_proposal(**args)
    assert first["state"] == "saved"
    await profile.save(owner, {"address": "陈姐"}, expected_revision=first["revision"])
    assert await write_profile_proposal(**args) == first
    assert (await profile.load(owner)).address == "陈姐"
    with pytest.raises(service.MemoryConflict):
        await profile.save(owner, {"address": "旧值"}, expected_revision=first["revision"])


async def test_conversational_correction_orders_by_current_sources_even_for_legacy_rows(monkeypatch):
    from db.models.message import Message
    owner, _, workspace, main, first = await setup(monkeypatch, "休息日喜欢去公园散步")
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        (await db.get(Message, first.id)).created_at = now - timedelta(minutes=10)
    args = dict(user_id=owner, workspace_id=workspace, session_id=main.id, verifier=Verifier())
    saved = await write_proposal(**args, message_ids=[first.id], operation_id="clock:initial",
        summary="休息日喜欢去公园散步", fact_key="personal.hobby")
    second = await create_user_message(main.id, "现在休息日喜欢在家做烘焙，不再去公园散步",
        user_id=owner, agent="assistant", origin="human", origin_ref={"actor_user_id": owner})
    async with get_db_session() as db:
        (await db.get(Message, second.id)).created_at = now - timedelta(minutes=1)
    changed = await write_proposal(**args, message_ids=[second.id], operation_id="clock:correction",
        memory_id=saved["memory_id"], expected_revision=saved["revision"], summary="休息日在家做烘焙，不再去公园散步")
    async with get_db_session() as db:
        row = await db.get(UserMemory, saved["memory_id"])
        assert service._utc(row.occurred_at) == now - timedelta(minutes=1)
        # Historical bug: the row retained the original assertion time while
        # current revision evidence correctly identifies the later statement.
        row.occurred_at = now - timedelta(minutes=10)
        await db.flush()
        assert (await service.assertion_times(db, [row]))[row.id] == now - timedelta(minutes=1)
    late = await create_user_message(main.id, "我休息日还是喜欢在公园走走，按这个推荐",
        user_id=owner, agent="assistant", origin="human", origin_ref={"actor_user_id": owner})
    async with get_db_session() as db:
        (await db.get(Message, late.id)).created_at = now - timedelta(minutes=5)
    with pytest.raises(service.MemoryConflict, match="older statement"):
        await write_proposal(**args, message_ids=[late.id], operation_id="clock:late-replay",
            memory_id=saved["memory_id"], expected_revision=changed["revision"], summary="休息日喜欢去公园散步")
    async with get_db_session() as db:
        row = await db.get(UserMemory, saved["memory_id"])
        assert row.value["summary"] == "休息日在家做烘焙，不再去公园散步"
        assert row.revision == changed["revision"]
