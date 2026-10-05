"""A second legitimate Session cannot rebind its prepared browser input.

Both Tasks, Driver/Inbox claims, provider checkpoints, control commands and
effect records are real. The existing Wuying fixture substitutes only guest
HTTP, the Chromium pipe and OSS; no QA, cloud or model request is issued.
"""
from dataclasses import replace
from types import SimpleNamespace

from sqlalchemy import select

from agent import inbox
from agent.driver import reserve_run
from assistant import control as task_controls
from assistant.commands import accept_task_command
from db.base import get_db_session
from db.models.agent_event import AgentEvent
from db.models.assistant import AssistantTask
from db.models.browser_resource import BrowserResourceSession
from db.models.external_effect import ExternalEffect
from models.message import TextPart
from sandbox.browser_operation import prepare, prepare_provider
from session.session import create_assistant_message, save_part, update_message_info
from tests.unit.test_assistant_browser_resources import browser_world, command  # noqa: F401
from tests.unit.test_private_browser_automation import (  # noqa: F401
    active, automation, call, capture, execute,
)
from tests.unit.test_private_wuying_runtime import (  # noqa: F401
    assistant_database, wuying_world as private_world,
)
from tool.tool import ToolContext


async def _settle_pause(w):
    answer = await create_assistant_message(w.ctx.session_id, w.user_message.id,
        agent="build", model_id=w.config.model, user_id=w.ctx.user_id,
        run_fence=w.ctx.run_fence)
    await save_part(TextPart(session_id=w.ctx.session_id, message_id=answer.id,
        text="Paused for the person's browser control"), is_new=True,
        user_id=w.ctx.user_id, run_fence=w.ctx.run_fence)
    answer.finish = "aborted"
    await update_message_info(answer, user_id=w.ctx.user_id, run_fence=w.ctx.run_fence)
    await inbox.settle_claimed_inbox_items(w.lease, result_message_id=answer.id, outcome="aborted")
    await w.lease.release(session_status="idle")


async def test_other_linked_session_cannot_dispatch_prepared_browser_call_after_control_change(active, monkeypatch, record_property):
    first = active
    await capture(first)  # A real checkpoint associates the first execution.
    other_task = await accept_task_command(**first.scope, project_id=first.main.project_id,
        idempotency_key="second-browser-session", prompt="Inspect the same actor-private browser.",
        model=first.config.model)
    lease = await reserve_run(other_task["execution_session_id"], first.w.owner)
    first.resumed.append(lease)  # Existing fixture releases every real Driver.
    batch = await inbox.claim_inbox_boundary(lease, step=1, include_next_turn=True)
    await lease.set_phase("running")
    other = SimpleNamespace(**vars(first))
    other.task, other.lease, other.user_message = other_task, lease, batch.messages[0]
    other.resource_id = await prepare_provider(lease)
    other.ctx = ToolContext(session_id=lease.session_id, user_id=lease.user_id,
        workspace_id=first.w.workspace, project_id=first.main.project_id,
        agent_id="build", run_id=lease.run_id, run_generation=lease.generation,
        abort=lease.abort, _assert_current=lease.assert_current)
    assert other.ctx.session_id != first.ctx.session_id
    assert other.ctx.user_id == first.ctx.user_id
    assert other.resource_id == first.resource_id
    await capture(other)  # Its own provider-visible frame, not the first's.
    old_args = {"action": "text", "text": "NEVER_FROM_OLD_SECOND_SESSION"}
    old_request, _ = await call(other, old_args)
    original, _, old_fence, _ = await prepare(other.ctx, old_args)
    old_context = replace(other.ctx)
    assert old_fence.epoch == 1 and original.state == "prepared"
    async with get_db_session() as db:
        links = set((await db.scalars(select(BrowserResourceSession.session_id).where(
            BrowserResourceSession.resource_id == first.resource_id))).all())
        tasks = list((await db.scalars(select(AssistantTask).where(AssistantTask.id.in_(
            (first.task["task_id"], other.task["task_id"]))))).all())
    assert {first.ctx.session_id, other.ctx.session_id} <= links
    assert len(tasks) == 2 and all(row.assistant_session_id == first.main.id and
        row.user_id == first.w.owner for row in tasks)

    recover = task_controls.recover_controls

    async def recover_without_model(**kwargs):
        changed, leases = await recover(**kwargs, launch=False)
        first.resumed.extend(leases)
        return changed, leases

    monkeypatch.setattr(task_controls, "recover_controls", recover_without_model)
    pending = await command(first, "takeover", 1, "two-session-takeover")
    assert pending.status_code == 200 and pending.json()["state"] == "draining", pending.text
    assert first.lease.abort.is_set() and other.lease.abort.is_set()
    for w in (first, other):
        await _settle_pause(w)
        await recover(task_id=w.task["task_id"], launch=False)
    grant = await command(first, "takeover", 1, "two-session-takeover")
    assert grant.status_code == 200 and grant.json()["fence"]["epoch"] == 2, grant.text
    assert first.supervisor.status()["control"]["fence"]["owner_kind"] == "human"
    returned = await command(first, "giveback", 2, "two-session-giveback")
    assert returned.status_code == 200 and returned.json()["fence"]["epoch"] == 3, returned.text
    assert set(returned.json()["resume_requested_task_ids"]) == {first.task["task_id"], other.task["task_id"]}
    resumed, = [row for row in first.resumed if row.session_id == other.ctx.session_id and row.run_id != lease.run_id]
    await inbox.claim_inbox_boundary(resumed, step=1, include_next_turn=True)
    await resumed.set_phase("running")
    other.ctx = replace(old_context, run_id=resumed.run_id, run_generation=resumed.generation,
        abort=resumed.abort, _assert_current=resumed.assert_current)
    await resumed.assert_current()  # The rejection below is not a stale Driver.
    before_http, before_pipe = list(first.requests), list(first.pipe.calls)
    denied = await execute(other, old_args)
    assert denied.metadata["error"] is True
    assert first.requests == before_http and first.pipe.calls == before_pipe
    assert first.pipe.text == ""
    async with get_db_session() as db:
        effect = await db.get(ExternalEffect, original.effect_id)
        original_request = await db.get(AgentEvent, old_request.id)
        assert effect.session_id == other.ctx.session_id and effect.resource_epoch == 1
        assert effect.state == "prepared" and effect.attempt_count == 0
        assert effect.safe_context["resource_request_id"] == old_request.payload["request_id"]
        assert original_request.payload["browser_context"] == old_request.payload["browser_context"]

    # Same current second Session remains usable after its new observation.
    await capture(other)
    fresh_args = {"action": "text", "text": "CURRENT_SECOND_SESSION"}
    fresh_request, _ = await call(other, fresh_args)
    assert fresh_request.payload["browser_context"]["fence"]["epoch"] == 3
    assert fresh_request.payload["browser_context"]["observation"] is not None
    assert not (await execute(other, fresh_args)).metadata.get("error")
    assert first.pipe.text == "CURRENT_SECOND_SESSION"
    assert [kind for kind, _ in first.pipe.calls] == ["capture", "capture", "capture", "text"]
    record_property("first_task_id", first.task["task_id"])
    record_property("second_task_id", other.task["task_id"])
    record_property("first_session_id", first.ctx.session_id)
    record_property("second_session_id", other.ctx.session_id)
    record_property("resource_id", first.resource_id)
    record_property("old_effect_id", original.effect_id)
    record_property("old_request_id", old_request.payload["request_id"])
    record_property("old_epoch", 1)
    record_property("current_epoch", 3)
