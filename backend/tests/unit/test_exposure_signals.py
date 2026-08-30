"""Exposure signals use owned backend state and never touch a sandbox."""

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent import exposure_signals as signals_mod
from agent.exposure_signals import collect_exposure_signals, extract_user_part_signals
from db.base import get_db_session
from db.models.file_asset import FileAsset
from db.models.project import Project
from db.models.session import Session
from db.models.user import User
from db.models.video_job import VideoJob
from db.models.video_production import VideoProduction
from models.message import FilePart, TextPart, TodoItem, TodoList


async def _return_false(*_args):
    return False


async def _return_empty(*_args):
    return ()


@pytest.fixture(autouse=True)
def _clear_product_state_lkg():
    signals_mod._PRODUCT_STATE_LKG.clear()
    yield
    signals_mod._PRODUCT_STATE_LKG.clear()


def _stub_state_probes(monkeypatch, *, keep_todo: bool = False) -> None:
    if not keep_todo:
        monkeypatch.setattr(signals_mod, "_has_open_todos", _return_false)
    monkeypatch.setattr(signals_mod, "_has_active_video_production", _return_false)
    monkeypatch.setattr(signals_mod, "_has_active_video_job", _return_false)
    monkeypatch.setattr(signals_mod, "_has_video_approval_state", _return_false)
    monkeypatch.setattr(signals_mod, "_has_video_recovery_state", _return_false)
    monkeypatch.setattr(signals_mod, "_deliverable_asset_ids", _return_empty)


def _stub_mutable_state_probes(monkeypatch, **overrides):
    state = {
        "todo": False,
        "production": False,
        "job": False,
        "approval": False,
        "recovery": False,
        "assets": (),
    }
    state.update(overrides)

    def probe(name):
        async def run(*_args):
            value = state[name]
            if isinstance(value, BaseException):
                raise value
            return value

        return run

    monkeypatch.setattr(signals_mod, "_has_open_todos", probe("todo"))
    monkeypatch.setattr(
        signals_mod,
        "_has_active_video_production",
        probe("production"),
    )
    monkeypatch.setattr(signals_mod, "_has_active_video_job", probe("job"))
    monkeypatch.setattr(
        signals_mod,
        "_has_video_approval_state",
        probe("approval"),
    )
    monkeypatch.setattr(
        signals_mod,
        "_has_video_recovery_state",
        probe("recovery"),
    )
    monkeypatch.setattr(signals_mod, "_deliverable_asset_ids", probe("assets"))
    return state


class _FakeClock:
    def __init__(self):
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


async def _seed_scope() -> tuple[str, str]:
    suffix = uuid4().hex[:12]
    user_id = f"signal_user_{suffix}"
    project_id = f"signal_project_{suffix}"
    session_id = f"signal_session_{suffix}"
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(User(id=user_id, username=user_id, created_at=now, updated_at=now))
        db.add(
            Project(
                id=project_id,
                user_id=user_id,
                name="Signal project",
                created_at=now,
                updated_at=now,
            )
        )
        db.add(
            Session(
                id=session_id,
                user_id=user_id,
                project_id=project_id,
                created_at=now,
                updated_at=now,
            )
        )
    return user_id, session_id


def test_last_user_parts_extract_text_urls_and_attachment_kinds():
    last_user = SimpleNamespace(
        parts=[
            TextPart(text="请核对 https://example.com/docs). 再看 https://example.com/docs"),
            TextPart(text="https://internal.invalid/ignored", synthetic=True),
            FilePart(path="/uploads/screen.png", mime_type="image/png"),
            FilePart(path="/uploads/spec.pdf", mime_type="application/pdf"),
            # An attachment's OSS URL is storage state, not a research URL.
            FilePart(path="/uploads/data.csv", url="https://oss.invalid/data.csv"),
        ]
    )

    text, urls, kinds = extract_user_part_signals(last_user)

    assert text == "请核对 https://example.com/docs). 再看 https://example.com/docs"
    assert urls == ("https://example.com/docs",)
    assert kinds == ("image", "pdf", "text")


@pytest.mark.asyncio
async def test_collector_never_gets_or_calls_a_sandbox(monkeypatch):
    _stub_state_probes(monkeypatch)
    calls: list[str] = []

    class CountingSandbox:
        def __getattr__(self, name):
            calls.append(f"sandbox:{name}")
            raise AssertionError("exposure collection must not touch the sandbox")

    fake = CountingSandbox()

    async def get_client(*_args, **_kwargs):
        calls.append("get_client")
        return fake

    async def get_client_any(*_args, **_kwargs):
        calls.append("get_client_any")
        return fake

    from sandbox import sandbox_manager

    monkeypatch.setattr(sandbox_manager, "get_client", get_client)
    monkeypatch.setattr(sandbox_manager, "get_client_any", get_client_any)

    result = await collect_exposure_signals(
        [TextPart(text="ordinary coding task")],
        session_id="session-no-sandbox",
        user_id="user-no-sandbox",
    )

    assert result.user_task_text == "ordinary coding task"
    assert calls == []


@pytest.mark.asyncio
async def test_probe_failures_are_independent_and_fail_small(monkeypatch):
    attempted: list[str] = []

    def failing(name):
        async def probe(*_args):
            attempted.append(name)
            raise RuntimeError(f"{name} unavailable")

        return probe

    monkeypatch.setattr(signals_mod, "_has_open_todos", failing("todo"))
    monkeypatch.setattr(signals_mod, "_has_active_video_production", failing("production"))
    monkeypatch.setattr(signals_mod, "_has_active_video_job", failing("job"))
    monkeypatch.setattr(signals_mod, "_has_video_approval_state", failing("approval"))
    monkeypatch.setattr(signals_mod, "_has_video_recovery_state", failing("recovery"))
    monkeypatch.setattr(signals_mod, "_deliverable_asset_ids", failing("asset"))

    result = await collect_exposure_signals(
        [TextPart(text="keep working")],
        session_id="session-db-down",
        user_id="user-db-down",
    )

    assert set(attempted) == {"todo", "production", "job", "approval", "recovery", "asset"}
    assert result.has_open_todos is False
    assert result.has_active_video_production is False
    assert result.has_active_video_job is False
    assert result.deliverable_asset_ids == ()
    assert result.signal_errors == (
        "todo_state_unavailable",
        "video_production_state_unavailable",
        "video_job_state_unavailable",
        "video_approval_state_unavailable",
        "video_recovery_state_unavailable",
        "deliverable_asset_state_unavailable",
    )


@pytest.mark.asyncio
async def test_partial_failure_uses_only_matching_lkg_without_caching_payloads(monkeypatch):
    asset_sentinel = "asset-sensitive-sentinel"
    text_sentinel = "user-text-sensitive-sentinel"
    state = _stub_mutable_state_probes(
        monkeypatch,
        production=True,
        job=True,
        assets=(asset_sentinel,),
    )
    scope = {"session_id": "session-lkg", "user_id": "user-lkg"}

    first = await collect_exposure_signals(
        [TextPart(text=text_sentinel)],
        **scope,
    )
    assert first.has_active_video_production is True
    assert first.has_active_video_job is True
    assert first.deliverable_asset_ids == (asset_sentinel,)

    cache_repr = repr(signals_mod._PRODUCT_STATE_LKG)
    assert text_sentinel not in cache_repr
    assert asset_sentinel not in cache_repr
    assert all(
        isinstance(item.value, bool)
        for values in signals_mod._PRODUCT_STATE_LKG.values()
        for item in values.values()
    )

    state["production"] = RuntimeError("production unavailable")
    state["job"] = False
    state["assets"] = RuntimeError("assets unavailable")
    second = await collect_exposure_signals(
        [TextPart(text="continue")],
        **scope,
    )

    assert second.has_active_video_production is True
    assert second.has_active_video_job is False
    assert second.deliverable_asset_ids == (signals_mod._DELIVERABLE_LKG_MARKER,)
    assert second.signal_errors == (
        "video_production_state_unavailable",
        "deliverable_asset_state_unavailable",
    )


@pytest.mark.asyncio
async def test_full_probe_outage_reuses_each_recent_value(monkeypatch):
    state = _stub_mutable_state_probes(
        monkeypatch,
        todo=True,
        production=True,
        job=False,
        approval=False,
        recovery=True,
        assets=("asset-not-cached",),
    )
    scope = {"session_id": "session-full-outage", "user_id": "user-full-outage"}
    primed = await collect_exposure_signals([], **scope)
    assert primed.has_open_todos is True
    assert primed.has_active_video_production is True
    assert primed.has_active_video_job is True
    assert primed.deliverable_asset_ids

    for name in state:
        state[name] = RuntimeError(f"{name} unavailable")
    fallback = await collect_exposure_signals([], **scope)

    assert fallback.has_open_todos is True
    assert fallback.has_active_video_production is True
    assert fallback.has_active_video_job is True
    assert fallback.deliverable_asset_ids == (signals_mod._DELIVERABLE_LKG_MARKER,)
    assert fallback.signal_errors == (
        "todo_state_unavailable",
        "video_production_state_unavailable",
        "video_job_state_unavailable",
        "video_approval_state_unavailable",
        "video_recovery_state_unavailable",
        "deliverable_asset_state_unavailable",
    )


@pytest.mark.asyncio
async def test_successful_false_replaces_recent_true(monkeypatch):
    state = _stub_mutable_state_probes(monkeypatch, production=True)
    scope = {"session_id": "session-false", "user_id": "user-false"}

    first = await collect_exposure_signals([], **scope)
    assert first.has_active_video_production is True

    state["production"] = False
    second = await collect_exposure_signals([], **scope)
    assert second.has_active_video_production is False

    state["production"] = RuntimeError("production unavailable")
    third = await collect_exposure_signals([], **scope)
    assert third.has_active_video_production is False
    assert third.signal_errors == ("video_production_state_unavailable",)


@pytest.mark.asyncio
async def test_product_state_lkg_isolated_by_user_and_session(monkeypatch):
    state = _stub_mutable_state_probes(monkeypatch, production=True)

    primed = await collect_exposure_signals(
        [],
        session_id="session-a",
        user_id="user-a",
    )
    assert primed.has_active_video_production is True

    state["production"] = RuntimeError("production unavailable")
    same_scope = await collect_exposure_signals(
        [],
        session_id="session-a",
        user_id="user-a",
    )
    other_session = await collect_exposure_signals(
        [],
        session_id="session-b",
        user_id="user-a",
    )
    other_user = await collect_exposure_signals(
        [],
        session_id="session-a",
        user_id="user-b",
    )

    assert same_scope.has_active_video_production is True
    assert other_session.has_active_video_production is False
    assert other_user.has_active_video_production is False


@pytest.mark.asyncio
async def test_product_state_lkg_expires_without_failure_refresh(monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr(signals_mod, "_PRODUCT_STATE_LKG_CLOCK", clock)
    state = _stub_mutable_state_probes(monkeypatch, production=True)
    scope = {"session_id": "session-ttl", "user_id": "user-ttl"}

    primed = await collect_exposure_signals([], **scope)
    assert primed.has_active_video_production is True

    state["production"] = RuntimeError("production unavailable")
    clock.advance(signals_mod._PRODUCT_STATE_LKG_TTL_SECONDS - 1)
    before_expiry = await collect_exposure_signals([], **scope)
    assert before_expiry.has_active_video_production is True

    clock.advance(2)
    after_expiry = await collect_exposure_signals([], **scope)
    assert after_expiry.has_active_video_production is False
    assert after_expiry.signal_errors == ("video_production_state_unavailable",)


@pytest.mark.asyncio
async def test_active_video_production_is_detected_without_video_words(monkeypatch):
    monkeypatch.setattr(signals_mod, "_has_open_todos", _return_false)
    user_id, session_id = await _seed_scope()
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(
            VideoProduction(
                id=f"production_{uuid4().hex[:12]}",
                user_id=user_id,
                session_id=session_id,
                title="Existing paid work",
                brief="Continue from durable state",
                status="needs_spend_approval",
                created_at=now,
                updated_at=now,
            )
        )

    result = await collect_exposure_signals(
        [TextPart(text="继续第三段")],
        session_id=session_id,
        user_id=user_id,
    )

    assert "视频" not in result.user_task_text
    assert result.has_active_video_production is True
    assert result.signal_errors == ()

    from agent.tool_exposure import route_intent_packs

    assert "video" in route_intent_packs(result)


@pytest.mark.asyncio
async def test_recovery_job_is_an_active_video_job_without_text_intent(monkeypatch):
    monkeypatch.setattr(signals_mod, "_has_open_todos", _return_false)
    user_id, session_id = await _seed_scope()
    now = datetime.now(timezone.utc)
    job_id = f"video_job_{uuid4().hex[:12]}"
    async with get_db_session() as db:
        db.add(
            VideoJob(
                id=job_id,
                user_id=user_id,
                session_id=session_id,
                kind="segment",
                idempotency_key=job_id,
                request_hash="hash",
                status="transfer_failed",
                request_data={},
                result_data={},
                created_at=now,
                updated_at=now,
            )
        )

    result = await collect_exposure_signals(
        [TextPart(text="查一下当前状态")],
        session_id=session_id,
        user_id=user_id,
    )

    assert result.has_active_video_job is True
    assert result.signal_errors == ()


@pytest.mark.asyncio
async def test_open_todo_comes_from_session_todo_service(monkeypatch):
    _stub_state_probes(monkeypatch, keep_todo=True)
    session_id = f"todo_signal_{uuid4().hex[:12]}"

    async def get_todo(requested_session_id):
        assert requested_session_id == session_id
        return TodoList(items=[TodoItem(subject="unfinished", status="in_progress")])

    import session.todo as todo_mod

    monkeypatch.setattr(todo_mod, "get_todo", get_todo)
    result = await collect_exposure_signals(
        [TextPart(text="continue")],
        session_id=session_id,
        user_id="todo-user",
    )

    assert result.has_open_todos is True


@pytest.mark.asyncio
async def test_deliverables_require_exact_user_and_session_ownership(monkeypatch):
    monkeypatch.setattr(signals_mod, "_has_open_todos", _return_false)
    user_id, session_id = await _seed_scope()
    now = datetime.now(timezone.utc)
    correct_id = f"asset_correct_{uuid4().hex[:10]}"
    other_user_id = f"signal_other_{uuid4().hex[:10]}"

    def asset(asset_id: str, **overrides) -> FileAsset:
        values = {
            "id": asset_id,
            "user_id": user_id,
            "session_id": session_id,
            "project_id": None,
            "name": f"{asset_id}.mp4",
            "oss_key": f"assets/{user_id}/{asset_id}/output.mp4",
            "mime": "video/mp4",
            "size": 100,
            "status": "ready",
            "source": "agent",
            "transient": False,
            "is_deleted": False,
            "created_at": now,
        }
        values.update(overrides)
        return FileAsset(**values)

    async with get_db_session() as db:
        db.add(
            User(
                id=other_user_id,
                username=other_user_id,
                created_at=now,
                updated_at=now,
            )
        )
        db.add_all([
            asset(correct_id),
            asset(f"asset_wrong_session_{uuid4().hex[:8]}", session_id="another-session"),
            asset(f"asset_wrong_user_{uuid4().hex[:8]}", user_id=other_user_id),
            asset(f"asset_user_upload_{uuid4().hex[:8]}", source="user"),
            asset(f"asset_transient_{uuid4().hex[:8]}", transient=True),
            asset(f"asset_pending_{uuid4().hex[:8]}", status="pending"),
            asset(f"asset_deleted_{uuid4().hex[:8]}", is_deleted=True),
        ])

    result = await collect_exposure_signals(
        [TextPart(text="继续")],
        session_id=session_id,
        user_id=user_id,
    )

    assert result.deliverable_asset_ids == (correct_id,)
    assert result.signal_errors == ()
