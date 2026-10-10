"""PA-01/02 acceptance through real HTTP adapters and independent SQL sessions."""
import asyncio
from copy import deepcopy
import json

from sqlalchemy import func, select

from assistant.service import ensure_main_session
from core.config import get_config
from db.base import get_db_session
from db.models.agent_inbox import AgentInboxItem
from db.models.assistant import AssistantCommand, AssistantTask, TaskSubmission
from db.models.session import Session
from tests.unit.test_assistant_api import client_for, signing_key  # noqa: F401
from tests.unit.test_assistant_foundation import accounts, assistant_database  # noqa: F401


async def retained_counts(owner, main_id, task_id):
    async with get_db_session() as db:
        return {name: await db.scalar(select(func.count()).select_from(model).where(predicate))
            for name, model, predicate in (
                ("commands", AssistantCommand, AssistantCommand.actor_user_id == owner),
                ("tasks", AssistantTask, AssistantTask.user_id == owner),
                ("submissions", TaskSubmission, TaskSubmission.task_id == task_id),
                ("inbox", AgentInboxItem, AgentInboxItem.user_id == owner),
                ("executions", Session, (Session.user_id == owner) & (Session.id != main_id)),
            )}


async def test_pa01_concurrent_http_creation_uses_exactly_one_remaining_quota_slot(monkeypatch, record_property):
    owner, _, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace, model="test/model")
    monkeypatch.setattr(get_config(), "max_sessions_per_user", 1)
    payload = {"idempotency_key": "one-slot", "project_id": main.project_id, "title": "One task",
               "input": {"text": "Retain one task", "delivery": "followup"}}
    async with client_for(owner, workspace, monkeypatch) as first_device, \
            client_for(owner, workspace, monkeypatch) as second_device:
        first, second = await asyncio.gather(first_device.post("/api/assistant/tasks", json=payload),
                                             second_device.post("/api/assistant/tasks", json=payload))
        assert first.status_code == second.status_code == 202
        receipt = first.json()
        assert receipt == second.json()
        # The occupied final slot must not prevent an exact retry. A genuinely
        # new command must hit the limit and roll back its tentative ledger row.
        assert (await second_device.post("/api/assistant/tasks", json=payload)).json() == receipt
        overflow = await first_device.post("/api/assistant/tasks", json={**payload, "idempotency_key": "another-task"})
        assert overflow.status_code == 429
    counts = await retained_counts(owner, main.id, receipt["task_id"])
    assert counts == {"commands": 1, "tasks": 1, "submissions": 1, "inbox": 1, "executions": 1}
    record_property("assistant_acceptance", json.dumps({"scenario": "PA-01", "main_id": main.id,
        "receipt": receipt, "responses": [first.status_code, second.status_code],
        "quota_limit": 1, "new_command_status": overflow.status_code, "counts": counts}))


async def test_pa02_lost_receipts_replay_after_newer_revision_and_changed_payloads_conflict(monkeypatch, record_property):
    owner, _, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace, model="test/model")
    payload = {"idempotency_key": "create", "project_id": main.project_id, "title": "Original task",
               "input": {"text": "Original request", "delivery": "followup"}}
    async with client_for(owner, workspace, monkeypatch) as client:
        created_response = await client.post("/api/assistant/tasks", json=payload)
        assert created_response.status_code == 202
        created = created_response.json()
        endpoint = f"/api/assistant/tasks/{created['task_id']}/commands"
        command = {"idempotency_key": "first-followup", "expected_revision": 1, "action": "input",
                   "input": {"text": "First correction", "delivery": "followup"}}
        accepted_response = await client.post(endpoint, json=command)
        assert accepted_response.status_code == 202
        accepted = accepted_response.json()
        newer_response = await client.post(endpoint, json={**command, "idempotency_key": "newer-followup",
            "expected_revision": 2, "input": {"text": "Newer correction", "delivery": "followup"}})
        assert newer_response.status_code == 202
        newer = newer_response.json()
        assert [created["task_revision"], accepted["task_revision"], newer["task_revision"]] == [1, 2, 3]
        assert created["execution_session_id"] == accepted["execution_session_id"] == newer["execution_session_id"]
        # Treat the first two responses as lost: retry their original requests
        # after the server has advanced, without adopting the newer revision.
        assert (await client.post("/api/assistant/tasks", json=payload)).json() == created
        assert (await client.post(endpoint, json=command)).json() == accepted
        assert (await client.get(f"/api/assistant/commands/{created['command_id']}")).json()["receipt"] == created
        assert (await client.get(f"/api/assistant/commands/{accepted['command_id']}")).json()["receipt"] == accepted
        changed_bodies = []
        for field, value in (("project_id", "different-project"), ("title", "Changed title"),
                             ("text", "Changed instructions"), ("attachment_ids", ["different-attachment"])):
            changed = deepcopy(payload)
            if field in {"project_id", "title"}:
                changed[field] = value
            else:
                changed["input"][field] = value
            response = await client.post("/api/assistant/tasks", json=changed)
            assert response.status_code == 409, (field, response.text)
            changed_bodies.append({"field": field, "status": response.status_code})
        assert (await client.post(endpoint, json={**command, "idempotency_key": "stale-new-command"})).status_code == 409
    counts = await retained_counts(owner, main.id, created["task_id"])
    assert counts == {"commands": 3, "tasks": 1, "submissions": 3, "inbox": 3, "executions": 1}
    record_property("assistant_acceptance", json.dumps({"scenario": "PA-02", "main_id": main.id,
        "original_receipts": [created, accepted], "latest_receipt": newer,
        "changed_bodies": changed_bodies, "stale_new_command_status": 409, "counts": counts}))
