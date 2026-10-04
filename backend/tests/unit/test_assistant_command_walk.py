"""Branch reuse preserves command graph limits and fresh root verification."""
from types import SimpleNamespace

import pytest
from sqlalchemy import select, update

from assistant.command_sources import FIELDS, _CommandWalk, validate_command_derivation
from assistant.policy import AssistantError
from assistant.service import ensure_main_session
from db.base import get_db_session
from db.models.session import Session
from tests.unit.test_assistant_foundation import accounts, assistant_database  # noqa: F401


async def graph(monkeypatch, db):
    owner, _, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace)
    await db.execute(select(1))
    commands, visits = {}, []

    def node(identity, children=()):
        proof = {"version": 1, "main_id": main.id, **{key: [] for key in FIELDS}}
        proof["business_reads"] = [{"node": child} for child in children]
        command = SimpleNamespace(id=identity, actor_user_id=owner, workspace_id=workspace,
            assistant_session_id=main.id, source_ref={"derivation": proof})
        commands[identity] = command
        return command

    async def descend(db, reads, **kwargs):
        visits.append(tuple(ref["node"] for ref in reads))
        for ref in reads:
            await validate_command_derivation(db, main, commands[ref["node"]],
                snapshot_checks=kwargs.get("snapshot_checks"))

    monkeypatch.setattr("assistant.evidence.validate_business_reads", descend)
    return main, node, commands, visits, descend


async def test_repeated_branches_are_checked_once_but_each_root_is_fresh(monkeypatch):
    async with get_db_session() as db:
        main, node, _, visits, descend = await graph(monkeypatch, db)
        node("leaf")
        root = node("root", ["leaf"] * 100)
        await validate_command_derivation(db, main, root)
        assert len(visits) == 2
        await validate_command_derivation(db, main, root)
        assert len(visits) == 4
        with monkeypatch.context() as patch:
            patch.setattr(_CommandWalk, "MAX_ENTRIES", 0)
            await validate_command_derivation(db, main, root)
        assert len(visits) == 105

        async def revoked(db, reads, **kwargs):
            if not reads:
                raise AssistantError(410, "REVOKED_TEST_SOURCE", "Original source changed")
            await descend(db, reads, **kwargs)

        monkeypatch.setattr("assistant.evidence.validate_business_reads", revoked)
        with pytest.raises(AssistantError, match="Original source changed"):
            await validate_command_derivation(db, main, root)


async def test_cached_shallow_branch_cannot_bypass_a_later_depth_limit(monkeypatch):
    async with get_db_session() as db:
        main, node, _, _, _ = await graph(monkeypatch, db)
        node("c0")
        for depth in range(1, 63):
            node(f"c{depth}", [f"c{depth - 1}"])
        node("extra", ["c62"])
        root = node("root", ["c62", "extra"])
        with pytest.raises(AssistantError) as rejected:
            await validate_command_derivation(db, main, root)
        assert rejected.value.code == "ASSISTANT_COMMAND_SOURCE_UNVERIFIED"


async def test_cached_descendants_still_reject_a_cycle_or_changed_scope(monkeypatch):
    async with get_db_session() as db:
        main, node, commands, _, descend = await graph(monkeypatch, db)
        node("leaf")
        node("branch", ["leaf"])
        root = node("root", ["branch", "leaf"])

        async def changed(db, reads, **kwargs):
            if reads == [{"node": "branch"}, {"node": "leaf"}]:
                await descend(db, reads[:1], **kwargs)
                commands["leaf"].source_ref["derivation"]["business_reads"] = [{"node": "branch"}]
                await descend(db, reads[1:], **kwargs)
            else:
                await descend(db, reads, **kwargs)

        monkeypatch.setattr("assistant.evidence.validate_business_reads", changed)
        with pytest.raises(AssistantError):
            await validate_command_derivation(db, main, root)
        monkeypatch.setattr("assistant.evidence.validate_business_reads", descend)
        commands["leaf"].actor_user_id = "another-owner"
        with pytest.raises(AssistantError):
            await validate_command_derivation(db, main, root)


@pytest.mark.parametrize("mutation", ["dirty", "flush", "bulk"])
async def test_a_write_disables_reuse_for_the_rest_of_the_walk(monkeypatch, mutation):
    async with get_db_session() as db:
        main, node, _, visits, descend = await graph(monkeypatch, db)
        node("leaf")
        root = node("root", ["leaf", "leaf"])

        async def changed(db, reads, **kwargs):
            if reads:
                await descend(db, reads[:1], **kwargs)
                if mutation == "bulk":
                    await db.execute(update(Session).where(Session.id == main.id).values(title="changed"))
                else:
                    (await db.get(Session, main.id)).title = "changed"
                    if mutation == "flush":
                        await db.flush()
                await descend(db, reads[1:], **kwargs)
            else:
                await descend(db, reads, **kwargs)

        monkeypatch.setattr("assistant.evidence.validate_business_reads", changed)
        await validate_command_derivation(db, main, root)
        assert visits.count(()) == 2
