"""What the memory learns about how a person likes to be helped, and how it keeps that tidy.

Style feedback becomes a lasting preference, a plan keeps its end date, the assistant's name never
becomes a memory, the same fact said twice is one memory, and core memories say each fact once.
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update

from core import config as runtime_config
from db.base import get_db_session
from db.models.memory import UserMemory
from db.models.memory_v2 import MemoryTombstone
from memory import service
from memory.curator import merge_duplicates, same_wording
from memory.extraction import (ExtractionSchemaError, MemoryExtractionWorker, said_at, valid_until_ttl,
                               validate_proposals)
from memory.grounding import GroundingVerifier, verify_memories
from memory.jobs import ExtractionInput
from memory.wiki.provider import ConfiguredWikiModel
from tests.unit.test_memory_pipeline import _finish_turn, _seed, pipeline_database  # noqa: F401
from tests.unit.test_memory_sensitive import provider
from wiki_compiler.hashing import canonical_hash

SAID = "太长了，以后说重点就行。这周五我要加班，家庭聚餐去不了。"
SAID_AT = "2026-10-08T07:30:00+00:00"  # a Thursday afternoon in Shanghai


def frozen(text=SAID):
    return ExtractionInput(job_id="job", user_id="user", workspace_id="ws", project_id=None, session_id="s",
        logical_turn_id="turn", input_hash=canonical_hash(text), acl_hash="acl", existing_memories=(),
        base_revisions=(), sources=({"source_kind": "user_statement", "body": text, "occurred_at": SAID_AT},))


def candidate(summary, quote, *, type="PREFERENCE", fact_key=None, valid_until=None, **extra):
    return {"type": type, "summary": summary, "fact_key": fact_key, "confidence": 90, "source_indexes": [0],
            "quotes": [{"source_index": 0, "quote": quote}], "valid_until": valid_until, **extra}


def test_a_plan_keeps_its_last_day_and_the_assistants_name_never_becomes_a_memory():
    kept = validate_proposals({"candidates": [
        candidate("用户嫌回答太长，希望先说结论", "太长了，以后说重点就行", type="FEEDBACK", fact_key="personal.style.length"),
        candidate("用户这周五要加班，去不了家庭聚餐", "这周五我要加班", type="USER_PROFILE", valid_until="2026-10-09"),
        candidate("用户给助理取名为小七", "以后说重点就行"),
        candidate("用户希望被称为老王", "以后说重点就行", fact_key="personal.address"),
    ]}, frozen())
    assert [(item["summary"], item["valid_until"]) for item in kept] == [
        ("用户嫌回答太长，希望先说结论", None), ("用户这周五要加班，去不了家庭聚餐", "2026-10-09")]
    # The first schema had no end date at all: still accepted.
    plain = {key: value for key, value in candidate("用户爱吃辣", "太长了").items() if key != "valid_until"}
    assert validate_proposals({"candidates": [plain]}, frozen())[0]["valid_until"] is None
    for bad in ("下周五", "2026-13-01", 20261009):
        with pytest.raises(ExtractionSchemaError):
            validate_proposals({"candidates": [candidate("用户这周五要加班", "这周五我要加班", valid_until=bad)]}, frozen())


def test_a_plan_lasts_to_the_end_of_its_last_day_where_the_user_lives():
    noon = datetime(2026, 10, 9, 4, 0, tzinfo=timezone.utc)  # Friday noon in Shanghai
    assert valid_until_ttl("2026-10-09", noon) == 12 * 3600  # until Friday midnight, Shanghai time
    assert valid_until_ttl("2026-10-08", noon) == 0           # already over: not kept
    assert valid_until_ttl(None, noon) is None
    assert said_at({"occurred_at": SAID_AT}) == "2026-10-08 15:30 Thursday (Asia/Shanghai)"
    assert said_at({"occurred_at": None}) is None


async def test_verification_checks_the_end_date_against_when_it_was_said():
    seen = []

    class Verifier:
        async def verify(self, items, *, purpose, model):
            seen.extend(items)
            return [True] * len(items), {}
    proposals = [candidate("用户这周五要加班", "这周五我要加班", valid_until="2026-10-09"),
                 candidate("用户爱吃辣", "太长了")]
    runtime_config.get_config().memory.extract_model = "test/model"
    await verify_memories(frozen(), proposals, runtime_config.get_config().memory, Verifier())
    assert seen[0]["claim"] == "用户这周五要加班 (until 2026-10-09)" and seen[1]["claim"] == "用户爱吃辣"
    assert seen[0]["sources"] == ["[said 2026-10-08 15:30 Thursday (Asia/Shanghai)] " + SAID]


async def test_style_feedback_and_a_plan_are_learned_with_their_kind_and_end(monkeypatch):
    seed = await _seed(monkeypatch)
    runtime_config.get_config().memory.automatic_knowledge = True
    client = provider(monkeypatch)
    await _finish_turn(seed, text=SAID)
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat()

    def extractor(frozen_input):
        return {"candidates": [
            candidate("用户嫌回答太长，希望先说结论", "太长了，以后说重点就行", type="FEEDBACK", fact_key="personal.style.length"),
            candidate("用户这周五要加班，去不了家庭聚餐", "这周五我要加班", type="USER_PROFILE", valid_until=tomorrow),
            candidate("用户上周要加班", "这周五我要加班", type="USER_PROFILE", valid_until="2020-01-03")]}
    verifier = GroundingVerifier(adapter=ConfiguredWikiModel(client=client))
    assert await MemoryExtractionWorker(extractor=extractor, verifier=verifier).run_once() == "SUCCEEDED"
    async with get_db_session() as db:
        rows = {row.value["summary"]: row for row in (await db.scalars(select(UserMemory).where(
            UserMemory.user_id == seed[0]))).all()}
    assert set(rows) == {"用户嫌回答太长，希望先说结论", "用户这周五要加班，去不了家庭聚餐"}  # a plan already over is dropped
    style = rows["用户嫌回答太长，希望先说结论"]
    assert (style.status, style.fact_key, style.project_id, style.ttl) == ("ACTIVE", "personal.style.length", None, None)
    plan = rows["用户这周五要加班，去不了家庭聚餐"]
    assert plan.status == "ACTIVE" and plan.ttl is not None
    ttl = plan.ttl if plan.ttl.tzinfo else plan.ttl.replace(tzinfo=timezone.utc)
    assert timedelta(hours=1) < ttl - datetime.now(timezone.utc) <= timedelta(days=2)


async def test_copies_of_one_fact_become_the_one_in_most_use_and_nothing_is_forgotten(monkeypatch):
    seed = await _seed(monkeypatch)
    user, workspace = seed[0], seed[1]
    first = await service.create_note(user_id=user, workspace_id=workspace, summary="用户周末一般会去游泳。")
    second = await service.create_note(user_id=user, workspace_id=workspace, summary="用户周末一般会去游泳")
    other = await service.create_note(user_id=user, workspace_id=workspace, summary="用户周日通常去看望父母")
    async with get_db_session() as db:
        await db.execute(update(UserMemory).where(UserMemory.id == second["id"]).values(hit_count=179))
    assert same_wording("用户周末一般会去游泳。") == same_wording("用户 周末一般会去游泳")
    assert await merge_duplicates(user, workspace) == 1
    assert await merge_duplicates(user, workspace) == 0  # nothing left to merge
    async with get_db_session() as db:
        status = {row.id: row.status for row in (await db.scalars(select(UserMemory).where(
            UserMemory.user_id == user))).all()}
        tombstones = (await db.scalars(select(MemoryTombstone).where(MemoryTombstone.user_id == user))).all()
    assert status == {first["id"]: "DEPRECATED", second["id"]: "ACTIVE", other["id"]: "ACTIVE"}
    assert tombstones == []  # a later correction of the same fact can still be learned


async def test_core_memories_say_each_fact_once_and_leave_style_to_the_users_section(monkeypatch):
    from memory.orchestrator import _stable_background
    from memory.policy import resolve_access_scope
    seed = await _seed(monkeypatch)
    user, workspace = seed[0], seed[1]
    for summary in ("用户对花生过敏。", "用户对花生过敏"):
        await service.create_note(user_id=user, workspace_id=workspace, summary=summary)
    await service.create_note(user_id=user, workspace_id=workspace, summary="用户嫌回答太长",
                              fact_key="personal.style.length")
    async with get_db_session() as db:
        scope = await resolve_access_scope(db, user_id=user, workspace_id=workspace)
    background = await _stable_background(scope, runtime_config.get_config().memory)
    texts = [item["text"] for item in background["items"]]
    assert len([text for text in texts if "花生" in text]) == 1 and not any("太长" in text for text in texts)


async def test_the_assistant_files_how_to_talk_as_style(monkeypatch):
    from assistant import memory_tools
    from assistant.service import ensure_main_session
    from tests.unit.test_assistant_foundation import accounts
    from tests.unit.test_assistant_sessions_v2 import main_turn
    monkeypatch.setattr("agent.inbox.schedule_inbox_wake", lambda *_: None)
    owner, _, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace, model="test/model")
    ctx, lease, _ = await main_turn(owner, workspace, main, "以后别说那么多客套话")
    try:
        from dataclasses import replace
        saved = await memory_tools.remember(replace(ctx, part_id="part-style"), summary="用户不喜欢客套话",
                                            quote="以后别说那么多客套话", about="style")
        fact = await memory_tools.remember(replace(ctx, part_id="part-fact"), summary="用户不喜欢香菜",
                                           quote="以后别说那么多客套话")
    finally:
        await lease.release(session_status="idle")
    async with get_db_session() as db:
        keys = {row.value["summary"]: row.fact_key for row in (await db.scalars(select(UserMemory).where(
            UserMemory.id.in_((saved["memory_id"], fact["memory_id"]))))).all()}
    assert keys["用户不喜欢客套话"].startswith("personal.style.note.") and keys["用户不喜欢香菜"] is None


async def test_a_thumbs_down_can_say_why(monkeypatch):
    from api import sessions as routes
    from assistant.service import ensure_main_session
    from db.models.message import Message
    from fastapi import HTTPException
    from models.message import TextPart
    from session.session import create_assistant_message, create_user_message, save_part, set_message_reaction
    from tests.unit.test_assistant_foundation import accounts
    owner, _, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace, model="test/model")
    user = await create_user_message(main.id, "问个事", agent="assistant", user_id=owner)
    reply = await create_assistant_message(main.id, user.id, user_id=owner)
    await save_part(TextPart(text="回答", session_id=main.id, message_id=reply.id), is_new=True, user_id=owner)

    async def stored():
        async with get_db_session() as db:
            row = await db.get(Message, reply.id)
            return row.reaction, row.reaction_reason
    await set_message_reaction(reply.id, main.id, "down", user_id=owner, reason="too_long")
    assert await stored() == ("down", "too_long")
    await set_message_reaction(reply.id, main.id, "up", user_id=owner)
    assert await stored() == ("up", None)  # a new reaction replaces the old reason
    for reaction, reason in (("up", "too_long"), ("down", "boring")):
        with pytest.raises(ValueError):
            await set_message_reaction(reply.id, main.id, reaction, user_id=owner, reason=reason)
    actor = {"user_id": owner, "workspace_id": workspace}
    answer = await routes.set_message_reaction(main.id, reply.id, routes.ReactionBody(reaction="down", reason="wrong"),
                                               current_user=actor)
    assert answer == {"ok": True, "reaction": "down", "reason": "wrong"} and await stored() == ("down", "wrong")
    with pytest.raises(HTTPException) as refused:
        await routes.set_message_reaction(main.id, reply.id, routes.ReactionBody(reaction="down", reason="meh"),
                                          current_user=actor)
    assert refused.value.status_code == 400
    from session.session import get_messages
    [shown] = [message for message in await get_messages(main.id, user_id=owner) if message.id == reply.id]
    assert (shown.reaction, shown.reaction_reason) == ("down", "wrong")  # every client sees why


async def test_a_reply_shows_the_memories_its_message_brought_up_as_they_read_now(monkeypatch):
    from api import memories as routes
    from assistant.service import ensure_main_session
    from fastapi import HTTPException
    from memory import recalls
    from tests.unit.test_assistant_foundation import accounts
    owner, other, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace, model="test/model")
    swim = await service.create_note(user_id=owner, workspace_id=workspace, summary="用户周末一般会去游泳")
    peanut = await service.create_note(user_id=owner, workspace_id=workspace, summary="用户对花生过敏")
    bundle = {"items": [{"kind": "memory", "id": swim["id"]}, {"kind": "wiki", "id": "page-1"},
                        {"kind": "memory", "id": peanut["id"]}, {"kind": "memory", "id": swim["id"]}],
              "stable_background": {"items": [{"kind": "memory", "id": "background-only"}]}}
    assert recalls.recalled_ids(bundle) == [swim["id"], peanut["id"]]  # relevant memories only, once each
    await recalls.record(user_id=owner, workspace_id=workspace, session_id=main.id, message_id="msg-1", bundle=bundle)
    await recalls.record(user_id=owner, workspace_id=workspace, session_id=main.id, message_id="msg-2",
                         bundle={"items": []})  # nothing relevant: nothing to show
    shown = await recalls.for_session(user_id=owner, workspace_id=workspace, session_id=main.id)
    assert {key: [item["summary"] for item in items] for key, items in shown.items()} == {
        "msg-1": ["用户周末一般会去游泳", "用户对花生过敏"]}
    await service.delete_memory(user_id=owner, workspace_id=workspace, memory_id=peanut["id"])
    answer = await routes.recalled(main.id, current_user={"user_id": owner, "workspace_id": workspace})
    assert [item["summary"] for item in answer["recalls"]["msg-1"]] == ["用户周末一般会去游泳"]  # forgotten: gone
    # Another member never sees the owner's memories; a chat nobody can open is not found.
    assert not await recalls.for_session(user_id=other, workspace_id=workspace, session_id=main.id)
    with pytest.raises(HTTPException) as missing:
        await routes.recalled("no-such-chat", current_user={"user_id": owner, "workspace_id": workspace})
    assert missing.value.status_code == 404
