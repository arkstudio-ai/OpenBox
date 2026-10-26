"""A durable forget must also revoke copies in a live provider conversation."""
import asyncio
from dataclasses import replace

from tests.support.voice_fakes import drain, item, event, started, done
from tests.unit.test_voice_bridge import replay
from tests.unit.test_voice_memory_context import call, packet, finish_tasks
from tests.unit.test_memory_read_protocol import context
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401


def revision(version, epoch):
    return {**packet("安排", version=version), "_revocation_epoch": epoch}


def history(bridge):
    bridge._memory_applied(revision(1, 0))
    bridge.memory_packet = revision(1, 0)
    bridge.spoken.item_created("old-human", "user", "message")
    bridge.spoken.item_created("old-answer", "assistant", "message")
    bridge.spoken.add("user", "周末做什么", "old-human")
    bridge.spoken.add("assistant", "周六青禾书屋读书会", "old-answer")
    bridge.keeper.summary = bridge.keeper.carried = "周六青禾书屋读书会"


async def test_idle_forget_waits_for_provider_deletion_and_excludes_all_reuse_paths():
    bridge, provider, _ = call()
    history(bridge)
    shared = []
    async def sink(lines):
        shared.append([line.text for line in lines])
    bridge.context_sink = sink
    async def refresh(*args):
        return revision(2, 1), "fresh"
    bridge.refresh_memory = refresh
    provider.deletion.clear()
    work = asyncio.create_task(bridge.refresh_idle_memory())
    await drain()
    assert {value[1] for value in provider.commands("delete")} == {"old-human", "old-answer"}
    assert not provider.commands("instructions") and bridge.memory_applied_version == 1
    provider.deletion.set()
    await work
    await drain()
    assert bridge.memory_applied_version == 2 and bridge._memory_revocation_epoch == 1
    assert bridge.spoken.items == [] and bridge.spoken.context_lines == []
    assert len(bridge.spoken.lines) == 2  # no mutation of the audit record
    assert not bridge.spoken.render() and not bridge.keeper.summary and not bridge.keeper.carried
    assert bridge._call_context("现在呢")["call"] == [] and shared[-1] == []
    assert not provider.commands("create") and not bridge.link.started
    await bridge.close()


async def test_addition_or_profile_change_does_not_clear_valid_conversation():
    bridge, provider, _ = call()
    history(bridge)
    async def refresh(*args):
        return revision(2, 0), "new setting or additional fact"
    bridge.refresh_memory = refresh
    await bridge.refresh_idle_memory()
    assert not provider.commands("delete")
    assert len(bridge.spoken.context_lines) == 2
    assert bridge.keeper.summary == "周六青禾书屋读书会"
    await bridge.close()


async def test_fresh_utterance_survives_revocation_before_first_answer():
    async def prepare(scope, text, recent):
        assert recent.epoch == 0
        return revision(2, 1), "fresh evidence"
    bridge, provider, _ = call(prepare)
    history(bridge)
    await replay(bridge, event("user_started", item_id="now"), item("now"), event("user_stopped"),
        started("auto"), done("auto", status="cancelled"),
        event("user_transcript", item_id="now", text="我周末有什么安排？"))
    await finish_tasks(bridge)
    assert ("delete", "now") not in provider.sent
    assert [line.item_id for line in bridge.spoken.context_lines] == ["now"]
    assert provider.commands("create") == [("create", None)]
    assert "青禾书屋" not in str(bridge._call_context("我周末有什么安排？"))
    await bridge.close()


async def test_unconfirmed_revocation_never_marks_new_epoch_or_answers():
    bridge, provider, _ = call()
    history(bridge)
    async def refresh(*args):
        return revision(2, 1), "fresh"
    async def rejected(identity):
        raise TimeoutError()
    bridge.refresh_memory = refresh
    provider.delete_item_confirmed = rejected
    await bridge.refresh_idle_memory()
    assert bridge.failed.is_set() and bridge._memory_revocation_epoch == 0
    assert not provider.commands("instructions") and not provider.commands("create")
    await bridge.close()


async def test_speech_during_idle_retirement_is_preserved_and_owns_next_answer():
    bridge, provider, _ = call()
    history(bridge)
    async def refresh(*args):
        return revision(2, 1), "fresh"
    bridge.refresh_memory = refresh
    provider.deletion.clear()
    work = asyncio.create_task(bridge.refresh_idle_memory())
    await drain()
    bridge._memory_new_speech()
    bridge.spoken.item_created("now", "user", "message")
    bridge.spoken.add("user", "新的要求", "now")
    provider.deletion.set()
    await work
    assert [line.item_id for line in bridge.spoken.context_lines] == ["now"]
    assert ("delete", "now") not in provider.sent and not provider.commands("instructions")
    assert bridge.memory_applied_version == 1
    await bridge.close()


async def test_sql_revocation_fences_old_recent_text_but_additions_preserve_it(monkeypatch):
    from db.base import get_db_session
    from memory import packet as packets, service
    from memory.versions import revocation_epoch
    config, turn = await context(monkeypatch)
    turn = replace(turn, entry="voice", recent_context=({"role": "assistant", "text": "旧日程"},),
                   recent_context_epoch=0)
    seen = []
    async def route(*args, **kwargs):
        seen.append(kwargs["recent_context"])
        return {"read_policy": "allowed", "memory_forbidden": False,
                "memory": {"needed": False}, "task": {"needed": False}}
    monkeypatch.setattr("memory.read_routing.route", route)
    note = await service.create_note(user_id=turn.user_id, workspace_id=turn.workspace_id, summary="周六读书")
    added = await packets.prepare_context(turn, config)
    assert added["_revocation_epoch"] == 0 and seen[-1] == turn.recent_context
    await service.edit_note(user_id=turn.user_id, workspace_id=turn.workspace_id, memory_id=note["id"],
                            summary="周日观鸟", expected_revision=note["revision"])
    corrected = await packets.prepare_context(turn, config)
    assert corrected["_revocation_epoch"] == 1 and seen[-1] == ()
    await service.delete_memory(user_id=turn.user_id, workspace_id=turn.workspace_id, memory_id=note["id"])
    async with get_db_session() as db:
        assert await revocation_epoch(db, turn.user_id, turn.workspace_id) == 2
        assert await revocation_epoch(db, "other-user", turn.workspace_id) == 0
        assert await revocation_epoch(db, turn.user_id, "other-workspace") == 0
    refreshed = await packets.refresh_packet(added, await _scope(turn), config)
    assert refreshed["_revocation_epoch"] == 2 and refreshed["_turn"].recent_context == ()


async def _scope(turn):
    from db.base import get_db_session
    from memory.readers import authority
    async with get_db_session() as db:
        return await authority(db, turn)
