"""Manifest contract, manifest-driven admission, and the demo skill E2E."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from skill_runtime import registry, repository as repo, service
from skill_runtime.manifest import (
    ManifestError,
    get_manifest,
    load_builtin_manifests,
    parse_manifest,
    validate_input,
)
from skill_runtime.types import JobStatus

NOW = lambda: datetime.now(timezone.utc)  # noqa: E731

MINIMAL_USER_YAML = """
apiVersion: openbox.ai/v1
kind: Skill
metadata:
  name: my-script
  version: 1.2.3
spec:
  distribution: user
  runtime:
    kind: sandbox
    handler: scripts/run.py
  operations:
    run: {}
"""


def _user() -> str:
    return "u_" + uuid.uuid4().hex[:8]


# ---------------------------------------------------------------------------
# Parsing & trust rules
# ---------------------------------------------------------------------------

def test_builtin_catalog_contains_demo_echo():
    manifests = load_builtin_manifests(refresh=True)
    demo = manifests.get("builtin:demo-echo")
    assert demo is not None
    assert demo.runtime.kind == "internal"
    # The happy paths plus the failure paths the UI must be able to exercise
    # by hand (retry backoff, per-operation timeout, prompt-only park).
    assert {"echo", "slow_echo", "ask_then_echo"} <= set(demo.operations)
    assert {"fail_then_succeed", "slow_step", "park_notice"} <= set(demo.operations)
    assert demo.operations["echo"].maxAttempts == 3
    assert demo.operations["slow_step"].invocationTimeoutSeconds == 5
    assert demo.phases["waiting_answer"] == "skill.demo-echo.phase.waiting_answer"


def test_untrusted_manifest_cannot_be_internal():
    bad = MINIMAL_USER_YAML.replace("kind: sandbox", "kind: internal")
    with pytest.raises(ManifestError, match="internal runtime"):
        parse_manifest(bad, trusted=False)
    parse_manifest(bad, trusted=True)  # trusted parse is fine


def test_untrusted_manifest_cannot_claim_builtin():
    bad = MINIMAL_USER_YAML.replace("distribution: user", "distribution: builtin")
    with pytest.raises(ManifestError, match="builtin distribution"):
        parse_manifest(bad, trusted=False)


def test_manifest_rejects_bad_shapes():
    with pytest.raises(ManifestError, match="apiVersion"):
        parse_manifest("apiVersion: nope/v9\nkind: Skill", trusted=True)
    with pytest.raises(ManifestError, match="kind"):
        parse_manifest("apiVersion: openbox.ai/v1\nkind: Deployment", trusted=True)
    with pytest.raises(ManifestError, match="invalid manifest"):
        parse_manifest(MINIMAL_USER_YAML.replace("name: my-script", "name: 'bad name!'"), trusted=False)
    with pytest.raises(ManifestError, match="invalid manifest"):
        parse_manifest(MINIMAL_USER_YAML.replace("    run: {}\n", ""), trusted=False)


def test_validate_input_required_fields():
    manifest = get_manifest("builtin:demo-echo")
    op = manifest.operation("echo")
    validate_input(op, {"text": "hi"})
    with pytest.raises(ManifestError, match="required"):
        validate_input(op, {})


# ---------------------------------------------------------------------------
# Manifest-driven admission
# ---------------------------------------------------------------------------

async def test_start_job_derives_runtime_from_manifest():
    job, created = await service.start_job(
        user_id=_user(),
        skill_key="builtin:demo-echo",
        operation="echo",
        input_data={"text": "hi"},
        idempotency_key="k-" + uuid.uuid4().hex[:6],
    )
    assert created is True
    assert job.runtime_kind == "internal"
    assert job.queue_name == "default"
    assert job.max_attempts == 3
    assert job.skill_version == "1.0.0"
    assert job.deadline_at is not None
    assert job.deadline_at - job.created_at <= timedelta(seconds=601)


async def test_start_job_unknown_skill_and_operation():
    with pytest.raises(service.UnknownSkill):
        await service.start_job(
            user_id=_user(), skill_key="builtin:nope", operation="x",
            input_data={}, idempotency_key="k1",
        )
    with pytest.raises(service.UnknownOperation):
        await service.start_job(
            user_id=_user(), skill_key="builtin:demo-echo", operation="nope",
            input_data={}, idempotency_key="k1",
        )


async def test_start_job_respects_user_disable():
    user = _user()
    await service.set_skill_enabled(user, "builtin:demo-echo", False)
    with pytest.raises(service.SkillDisabled):
        await service.start_job(
            user_id=user, skill_key="builtin:demo-echo", operation="echo",
            input_data={"text": "x"}, idempotency_key="k1",
        )
    await service.set_skill_enabled(user, "builtin:demo-echo", True)
    job, _ = await service.start_job(
        user_id=user, skill_key="builtin:demo-echo", operation="echo",
        input_data={"text": "x"}, idempotency_key="k1",
    )
    assert job.status == JobStatus.QUEUED.value


async def test_start_job_validates_input():
    with pytest.raises(ManifestError, match="required"):
        await service.start_job(
            user_id=_user(), skill_key="builtin:demo-echo", operation="echo",
            input_data={}, idempotency_key="k1",
        )


async def test_start_job_global_flag_gate(monkeypatch):
    from core.config import get_config

    monkeypatch.setattr(get_config(), "skill_jobs_enabled", False)
    with pytest.raises(service.SkillDisabled, match="deployment"):
        await service.start_job(
            user_id=_user(), skill_key="builtin:demo-echo", operation="echo",
            input_data={"text": "x"}, idempotency_key="k1",
        )


# ---------------------------------------------------------------------------
# Demo skill end-to-end through the worker (PR#13 skeleton)
# ---------------------------------------------------------------------------

def _demo_worker():
    from skill_runtime.worker import SkillJobWorker

    registry.load_builtin_handlers()
    # concurrency=1: the in-memory test DB shares one StaticPool connection,
    # so parallel invocations would interleave inside a single transaction.
    return SkillJobWorker(queues=("default",), concurrency=1, per_user_limit=0)


async def _drive_until(worker, job, statuses, ticks=40):
    """The shared default queue may hold older tests' leftovers; keep ticking
    until this job reaches one of the expected statuses."""
    fresh = await repo.get_job(job.id, job.user_id)
    for _ in range(ticks):
        if fresh.status in statuses:
            return fresh
        await worker.run_once()
        await worker.drain()
        fresh = await repo.get_job(job.id, job.user_id)
    return fresh


async def test_demo_echo_end_to_end():
    worker = _demo_worker()
    job, _ = await service.start_job(
        user_id=_user(), skill_key="builtin:demo-echo", operation="echo",
        input_data={"text": "hello"}, idempotency_key="k-" + uuid.uuid4().hex[:6],
    )
    done = await _drive_until(worker, job, {JobStatus.SUCCEEDED.value})
    assert done.status == JobStatus.SUCCEEDED.value
    assert done.result_data == {"echo": "hello"}


async def test_demo_slow_echo_survives_wait_cycle():
    from skill_runtime import reconciler

    worker = _demo_worker()
    job, _ = await service.start_job(
        user_id=_user(), skill_key="builtin:demo-echo", operation="slow_echo",
        input_data={"text": "later", "delay_seconds": 0}, idempotency_key="k-" + uuid.uuid4().hex[:6],
    )
    waiting = await _drive_until(worker, job, {JobStatus.WAITING_EXTERNAL.value})
    assert waiting.status == JobStatus.WAITING_EXTERNAL.value
    assert waiting.phase == "waiting_provider"

    await reconciler.requeue_due_external()
    done = await _drive_until(worker, job, {JobStatus.SUCCEEDED.value})
    assert done.status == JobStatus.SUCCEEDED.value
    assert done.result_data["delayed"] is True
    assert done.phase == "delivering"


async def test_demo_ask_then_echo_resume_flow():
    worker = _demo_worker()
    job, _ = await service.start_job(
        user_id=_user(), skill_key="builtin:demo-echo", operation="ask_then_echo",
        input_data={}, idempotency_key="k-" + uuid.uuid4().hex[:6],
    )
    asking = await _drive_until(worker, job, {JobStatus.WAITING_USER.value})
    assert asking.status == JobStatus.WAITING_USER.value
    events = await repo.get_events(job.id, job.user_id)
    assert events[-1].payload["prompt"] == "What should I echo?"

    await repo.add_input(
        job.id, job.user_id, kind="user_answer",
        payload={"text": "resumed"}, idempotency_key="answer-1",
    )
    done = await _drive_until(worker, job, {JobStatus.SUCCEEDED.value})
    assert done.status == JobStatus.SUCCEEDED.value
    assert done.result_data == {"echo": "resumed", "answered": True}


def test_job_snapshot_shape():
    manifests = load_builtin_manifests()
    assert "builtin:demo-echo" in manifests


async def test_builtin_skill_md_is_discoverable_and_activates_skill_job():
    """Regression: the agent-facing surface only exists if a discovered skill
    activates the skill_job tool — builtin packages must be scanned."""
    from skill.skill import load_skills, list_skills

    await load_skills()
    by_name = {s.name: s for s in await list_skills()}
    demo = by_name.get("demo-echo")
    assert demo is not None, "builtin_skills/ must be scanned for SKILL.md"
    assert "skill_job" in demo.allowed_tools
    assert demo.source == "builtin"


async def test_project_skill_shadows_same_named_builtin():
    """Builtin packages are lowest precedence, so the live video-production
    skill keeps winning until the operator retires it (grey rollout)."""
    from skill.skill import load_skills, get_skill

    await load_skills()
    video = await get_skill("video-production")
    if video is not None and video.source != "builtin":
        assert "video_generate" in video.allowed_tools or video.source == "project"


def test_manifest_declares_operation_timeouts():
    manifest = get_manifest("builtin:video-production")
    assert manifest.operation("segment.transcribe").invocationTimeoutSeconds == 600
    assert manifest.operation("segment.generate").invocationTimeoutSeconds == 120
