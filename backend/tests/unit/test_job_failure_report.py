"""A background job that dies must say so, and must not die slowly.

Three faults met in one screenshot: a card reading "handler raised
MaterialProviderError" — the class name, with the sentence naming the config
key to change thrown away — while the runtime spent twenty minutes of
exponential backoff retrying a misconfiguration that could never succeed, and
told no one when it finally gave up.
"""
import uuid

import pytest
from sqlalchemy import select

from db.base import get_db_session
from db.models.session_inbox import SessionInbox
from skill_runtime import repository as repo
from skill_runtime.inbox import FAILED_KIND, _is_write_back_kind, _source_state_ok
from skill_runtime.types import (
    Failed,
    HandlerError,
    JobStatus,
    error_is_retryable,
    public_error_text,
)


# ── what a handler is allowed to publish ────────────────────────────────────

def test_an_ordinary_exception_never_publishes_its_text():
    """Provider bodies echo prompts, signed URLs and sometimes credentials."""
    assert public_error_text(RuntimeError("token=sk-live url=https://signed…")) is None
    # And it keeps the ordinary retry budget: it may well be transient.
    assert error_is_retryable(RuntimeError("connection reset")) is True


def test_a_handler_may_opt_its_own_message_in():
    err = HandlerError("relay does not serve /api/material; set material_base_url")
    assert public_error_text(err) == "relay does not serve /api/material; set material_base_url"


def test_a_handler_may_declare_a_fault_permanent():
    assert error_is_retryable(HandlerError("bad config", retryable=False)) is False
    assert error_is_retryable(HandlerError("flaky upstream")) is True


def test_material_errors_publish_only_what_we_wrote():
    """The class carries both kinds of message; only ours may be shown."""
    from video.materials import MaterialProviderError

    ours = MaterialProviderError("请配置 material_base_url", retryable=False, public=True)
    assert public_error_text(ours) == "请配置 material_base_url"
    assert error_is_retryable(ours) is False

    # Built from the provider's response body — must stay server-side, and it
    # is the default, so reusing this class cannot leak by omission.
    relayed = MaterialProviderError("provider said: token=sk-secret")
    assert public_error_text(relayed) is None
    assert error_is_retryable(relayed) is True


@pytest.mark.asyncio
async def test_the_configured_relay_error_is_permanent_and_speaks():
    """The exact fault from the report: a relay with no material endpoint."""
    from core.config import ProviderConfig, get_config
    from video.materials import MaterialProviderError, configured_material_target

    config = get_config()
    original = config.provider.get("doubao")
    config.provider["doubao"] = ProviderConfig(
        api_key="k", base_url="https://openapi.bossipai.com.cn"
    )
    try:
        with pytest.raises(MaterialProviderError) as excinfo:
            configured_material_target()
    finally:
        if original is not None:
            config.provider["doubao"] = original

    err = excinfo.value
    assert error_is_retryable(err) is False, "waiting cannot grow an endpoint"
    text = public_error_text(err)
    assert text and "material_base_url" in text, "must name the key to change"


# ── the failure notice ──────────────────────────────────────────────────────

async def _failed_job(*, session_id: str | None):
    job, _ = await repo.admit_job(
        user_id="u_" + uuid.uuid4().hex[:8],
        skill_key="builtin:demo-echo",
        operation="echo",
        runtime_kind="internal",
        input_data={"text": "x"},
        idempotency_key="k-" + uuid.uuid4().hex[:8],
        queue_name="q_" + uuid.uuid4().hex[:8],
        session_id=session_id,
    )
    claimed = await repo.claim_next(
        queues=(job.queue_name,), worker_id="w1", lease_seconds=60, limit=1
    )
    await repo.settle_invocation(
        job.id,
        claimed[0].lease_token,
        Failed(error_code="handler_permanent", message="relay has no /api/material"),
    )
    return job


async def _notices(job_id: str) -> list[SessionInbox]:
    async with get_db_session() as db:
        return list(
            (
                await db.execute(
                    select(SessionInbox).where(SessionInbox.source_job_id == job_id)
                )
            ).scalars()
        )


@pytest.mark.asyncio
async def test_a_failed_job_wakes_its_session():
    """Otherwise it dies in a card nobody is watching."""
    session_id = "session_" + uuid.uuid4().hex[:10]
    job = await _failed_job(session_id=session_id)

    notices = await _notices(job.id)
    assert len(notices) == 1
    notice = notices[0]
    assert notice.kind == FAILED_KIND
    assert notice.status == "pending"
    # Everything the model needs to judge the fault without another lookup.
    assert notice.payload["error_code"] == "handler_permanent"
    assert notice.payload["message"] == "relay has no /api/material"
    assert notice.payload["input"] == {"text": "x"}
    assert "attempts" in notice.payload


@pytest.mark.asyncio
async def test_failure_notices_do_not_depend_on_an_opt_in():
    """Success is opt-in because it costs tokens; a failure always needs telling."""
    session_id = "session_" + uuid.uuid4().hex[:10]
    job = await _failed_job(session_id=session_id)
    # admit_job above never set continue_agent_on_success.
    assert len(await _notices(job.id)) == 1


@pytest.mark.asyncio
async def test_a_job_with_no_session_has_nowhere_to_report():
    job = await _failed_job(session_id=None)
    assert await _notices(job.id) == []


@pytest.mark.asyncio
async def test_exactly_one_notice_per_failure():
    """One wake-up per dead job; a corrected retry is a new job with its own."""
    session_id = "session_" + uuid.uuid4().hex[:10]
    job = await _failed_job(session_id=session_id)
    assert len(await _notices(job.id)) == 1


def test_a_terminal_notice_is_owed_no_agent_result():
    """The job is already settled; writing an input into it would be rejected."""
    assert _is_write_back_kind(SessionInbox(kind=FAILED_KIND)) is False
    assert _is_write_back_kind(SessionInbox(kind="job_needs_agent")) is True


def test_a_failure_notice_is_judged_against_a_failed_job():
    """The completed rule would expire it the instant it was written."""
    assert _source_state_ok(FAILED_KIND, JobStatus.FAILED.value, None) is True
    assert _source_state_ok(FAILED_KIND, JobStatus.SUCCEEDED.value, None) is False


def test_the_failure_prompt_does_not_invite_a_blind_retry():
    """A wake-up that says "try again" is how this becomes a spend loop."""
    from skill_runtime.inbox import _failure_prompt

    prompt = _failure_prompt(
        SessionInbox(
            id="sinb_1",
            source_job_id="sjob_1",
            kind=FAILED_KIND,
            payload={
                "skill": "builtin:video-production",
                "operation": "segment.generate",
                "error_code": "handler_permanent",
                "message": "relay has no /api/material",
                "attempts": 3,
                "input": {"segment_id": "seg_1"},
            },
        )
    )
    assert "relay has no /api/material" in prompt
    assert "seg_1" in prompt
    assert "已重试 3 次" in prompt
    # The two instructions that keep a correction from becoming a loop.
    assert "不要用相同参数重新提交" in prompt
    assert "该走的审批照走" in prompt
