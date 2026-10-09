"""Project media survive result settlement, report retries, events and reloads."""
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from agent import inbox
from agent.driver import reserve_run
from assistant.commands import accept_task_command
from assistant.results import deliver_task_result
from db.base import get_db_session
from db.models.assistant import TaskResult
from db.models.file_asset import FileAsset
from db.models.part import Part
from db.models.project import Project
from models.message import FilePart, FileRelation, TextPart
from session.agent_event_log import verify_agent_event_parity
from session.session import create_assistant_message, save_part, update_message_info
from tests.unit.test_assistant_commands import setup_task
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401


async def output(owner, workspace, project, lease, message, name, *, kind="generated_image",
                 role="result", transient=False, source="agent", asset_owner=None, status="ready"):
    from core.identifier import generate_id
    asset = FileAsset(id=generate_id(), user_id=asset_owner or owner, workspace_id=workspace,
        session_id=lease.session_id, project_id=project, name=name, oss_key=f"test/{name}",
        mime="video/mp4" if name.endswith(".mp4") else "image/png", size=100,
        status=status, source=source, transient=transient, created_at=datetime.now(timezone.utc))
    async with get_db_session() as db:
        db.add(asset)
    part = FilePart(session_id=lease.session_id, message_id=message.id, asset_id=asset.id,
        path=f"/workspace/{name}", mime_type=asset.mime, url="https://expired.invalid/?signature=secret",
        oss_key=asset.oss_key, transient=transient,
        relation=FileRelation(kind=kind, role=role, label=name, source_part_id="project-tool"))
    await save_part(part, is_new=True, user_id=owner,
                    run_fence=(lease.session_id, lease.run_id, lease.generation))
    return asset, part


async def execution(kwargs, *, project=None, media=True):
    from core.identifier import generate_id
    args = {**kwargs, "idempotency_key": generate_id()}
    if project:
        now = datetime.now(timezone.utc)
        row = Project(id=generate_id(), user_id=args["user_id"], workspace_id=args["workspace_id"],
            name=project, created_at=now, updated_at=now)
        async with get_db_session() as db:
            db.add(row)
        args["project_id"] = row.id
    accepted = await accept_task_command(**args)
    lease = await reserve_run(accepted["execution_session_id"], args["user_id"])
    batch = await inbox.claim_inbox_boundary(lease, step=1, include_next_turn=True)
    fence = (lease.session_id, lease.run_id, lease.generation)
    tool_message = await create_assistant_message(lease.session_id, batch.messages[0].id,
        model_id="test/model", agent="build", user_id=args["user_id"], run_fence=fence)
    assets = []
    if media:
        for name, kind, role in [("poster.png", "generated_image", "result"), ("clip.mp4", "video_final", "final")]:
            assets.append(await output(args["user_id"], args["workspace_id"], args["project_id"],
                lease, tool_message, name, kind=kind, role=role))
    return args, accepted, lease, batch, tool_message, assets


async def settle(args, accepted, lease, batch, tool_message):
    fence = (lease.session_id, lease.run_id, lease.generation)
    tool_message.finish = "tool_calls"
    await update_message_info(tool_message, user_id=args["user_id"], run_fence=fence)
    final = await create_assistant_message(lease.session_id, batch.messages[0].id,
        model_id="test/model", agent="build", user_id=args["user_id"], run_fence=fence)
    await save_part(TextPart(session_id=lease.session_id, message_id=final.id, text="Media ready."),
        is_new=True, user_id=args["user_id"], run_fence=fence)
    final.finish = "stop"
    await update_message_info(final, user_id=args["user_id"], run_fence=fence)
    await inbox.settle_claimed_inbox_items(lease, result_message_id=final.id, outcome="succeeded")
    await lease.release(session_status="idle")
    async with get_db_session() as db:
        return await db.scalar(select(TaskResult).where(TaskResult.task_id == accepted["task_id"])
                               .order_by(TaskResult.created_at.desc()).limit(1))


async def report(owner, main, result):
    await deliver_task_result(result.id)
    lease = await reserve_run(main.id, owner)
    batch = await inbox.claim_inbox_boundary(lease, step=1, include_next_turn=True)
    fence = (main.id, lease.run_id, lease.generation)
    message = await create_assistant_message(main.id, batch.messages[0].id,
        model_id="test/model", agent="assistant", user_id=owner, run_fence=fence)
    await save_part(TextPart(session_id=main.id, message_id=message.id, text="Your project media are ready."),
        is_new=True, user_id=owner, run_fence=fence)
    return lease, message, fence


async def files(message):
    async with get_db_session() as db:
        return list((await db.scalars(select(Part).where(Part.message_id == message.id, Part.type == "file"))).all())


async def test_tool_step_media_are_forwarded_once_and_retained_in_canonical_history(monkeypatch):
    owner, _, _, main, kwargs = await setup_task()
    args, accepted, lease, batch, tool_message, assets = await execution(kwargs, project="Design project")
    # A repeated reference to the same output must not duplicate the gallery.
    duplicate = assets[0][1].model_copy(update={"id": FilePart().id})
    await save_part(duplicate, is_new=True, user_id=owner,
        run_fence=(lease.session_id, lease.run_id, lease.generation))
    result = await settle(args, accepted, lease, batch, tool_message)
    assert {p.id for _, p in assets} <= {r["part_id"] for r in result.output_refs}
    report_lease, message, fence = await report(owner, main, result)
    published = []
    monkeypatch.setattr("session.session.bus.publish", lambda name, data: published.append((name, data)))
    try:
        message.finish = "stop"
        await update_message_info(message, user_id=owner, run_fence=fence)
        await update_message_info(message, user_id=owner, run_fence=fence)
        copied = await files(message)
        assert {p.data["asset_id"] for p in copied} == {a.id for a, _ in assets}
        assert len(copied) == 2
        assert all(p.session_id == main.id and not p.data.get("url") and not p.data.get("oss_key") for p in copied)
        assert all(p.data["relation"]["metadata"]["assistant_source"]["project_name"] == "Design project" for p in copied)
        emitted = [data for name, data in published if name == "part.created"]
        assert {d["part"]["id"] for d in emitted} == {p.id for p in copied} and len(emitted) == 2
        assert all(d["userId"] == owner and d["sessionId"] == main.id for d in emitted)
        assert (await verify_agent_event_parity(main.id, user_id=owner)).ok
        async with get_db_session() as db:
            saved = await db.get(TaskResult, result.id)
            assert saved.processed_message_id == message.id and saved.delivery_state == "processed"
            assert await db.get(Part, assets[0][1].id) is not None
    finally:
        await report_lease.release(session_status="idle")


async def test_other_projects_and_prior_turns_cannot_leak_into_a_result():
    owner, _, _, main, kwargs = await setup_task()
    first = await execution(kwargs, project="First project")
    first_result = await settle(*first[:5])
    second = await execution(kwargs, project="Second project")
    second_result = await settle(*second[:5])
    for source, result in [(first, first_result), (second, second_result)]:
        lease, message, fence = await report(owner, main, result)
        message.finish = "stop"
        await update_message_info(message, user_id=owner, run_fence=fence)
        assert {p.data["asset_id"] for p in await files(message)} == {a.id for a, _ in source[5]}
        await lease.release(session_status="idle")
    # Follow up on the first project session: its previous media aren't new outputs.
    from db.models.assistant import AssistantTask
    async with get_db_session() as db:
        task = await db.get(AssistantTask, first[1]["task_id"])
    followup = await execution({**first[0], "task_id": task.id, "expected_revision": task.control_revision}, media=False)
    result = await settle(*followup[:5])
    lease, message, fence = await report(owner, main, result)
    message.finish = "stop"
    await update_message_info(message, user_id=owner, run_fence=fence)
    assert await files(message) == []
    await lease.release(session_status="idle")


async def test_unavailable_private_inputs_and_screenshots_are_not_forwarded():
    owner, other, workspace, main, kwargs = await setup_task()
    args, accepted, lease, batch, tool_message, assets = await execution(kwargs)
    for name, options in [
        ("screen.png", {"transient": True}), ("inspection.png", {"kind": "inspection_image"}),
        ("evidence.png", {"role": "evidence"}), ("input.png", {"role": "input"}),
        ("upload.png", {"source": "user"}), ("pending.png", {"status": "pending"}),
        ("other-user.png", {"asset_owner": other}),
    ]:
        await output(owner, workspace, args["project_id"], lease, tool_message, name, **options)
    result = await settle(args, accepted, lease, batch, tool_message)
    report_lease, message, fence = await report(owner, main, result)
    async with get_db_session() as db:
        (await db.get(FileAsset, assets[0][0].id)).is_deleted = True
    message.finish = "stop"
    await update_message_info(message, user_id=owner, run_fence=fence)
    assert [p.data["asset_id"] for p in await files(message)] == [assets[1][0].id]
    await report_lease.release(session_status="idle")


async def test_owned_media_reused_from_another_session_can_be_explicitly_returned():
    owner, _, _, main, kwargs = await setup_task()
    source = await execution(kwargs)
    async with get_db_session() as db:
        for asset, _ in source[5]:
            (await db.get(FileAsset, asset.id)).session_id = "original-project-session"
    result = await settle(*source[:5])
    lease, message, fence = await report(owner, main, result)
    message.finish = "stop"
    await update_message_info(message, user_id=owner, run_fence=fence)
    assert {p.data["asset_id"] for p in await files(message)} == {a.id for a, _ in source[5]}
    await lease.release(session_status="idle")


async def test_failed_report_transaction_emits_no_media_and_retry_attaches_once(monkeypatch):
    owner, _, _, main, kwargs = await setup_task()
    source = await execution(kwargs)
    result = await settle(*source[:5])
    lease, message, fence = await report(owner, main, result)
    from assistant import reporting
    append = reporting.append_agent_event_locked
    published = []
    monkeypatch.setattr("session.session.bus.publish", lambda name, data: published.append((name, data)))
    async def fail(*args, **kwargs):
        if kwargs["kind"] == "inbox.settled":
            raise RuntimeError("simulated report commit failure")
        return await append(*args, **kwargs)
    monkeypatch.setattr(reporting, "append_agent_event_locked", fail)
    message.finish = "stop"
    with pytest.raises(RuntimeError, match="report commit failure"):
        await update_message_info(message, user_id=owner, run_fence=fence)
    assert await files(message) == [] and not published
    monkeypatch.setattr(reporting, "append_agent_event_locked", append)
    await update_message_info(message, user_id=owner, run_fence=fence)
    assert len(await files(message)) == 2
    await lease.release(session_status="idle")
