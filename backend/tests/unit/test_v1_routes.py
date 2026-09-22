"""``/v1`` routes end to end over the ASGI app: contract shapes and refusals."""
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from api.v1 import files as files_api
from api.v1.app import create_v1_app
from auth import api_key as keys
from auth.middleware import get_current_user
from cache import set_cache
from cache.memory_cache import MemoryCache
from core.identifier import ascending
from db.base import get_db_session
from db.models.billing import UsageEvent
from db.models.file_asset import FileAsset
from db.models.session import Session as SessionRow
from db.models.user import User
from db.models.workspace import Workspace, WorkspaceMember


async def seed_scope() -> tuple[str, str]:
    uid, wid = f"u-{uuid4().hex[:10]}", f"w-{uuid4().hex[:10]}"
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(User(id=uid, username=uid, role="user", is_active=True, failed_login_count=0,
                    created_at=now, updated_at=now))
        await db.flush()
        db.add(Workspace(id=wid, name="ws", owner_user_id=uid, created_at=now, updated_at=now))
        await db.flush()
        db.add(WorkspaceMember(workspace_id=wid, user_id=uid, role="owner", status="active",
                               created_at=now, updated_at=now))
        await db.flush()
        (await db.get(User, uid)).default_workspace_id = wid
    return uid, wid


def key_identity(uid: str, wid: str, **overrides) -> dict:
    identity = {
        "user_id": uid, "role": "user", "workspace_id": wid, "workspace_role": "owner",
        "auth_kind": "api_key", "api_key_id": f"key-{uuid4().hex[:8]}",
        "scopes": list(keys.DEFAULT_SCOPES), "policy": dict(keys.DEFAULT_POLICY), "rate_limit": None,
    }
    identity.update(overrides)
    return identity


def client(identity: dict) -> httpx.AsyncClient:
    app = create_v1_app()
    app.dependency_overrides[get_current_user] = lambda: identity
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.fixture(autouse=True)
def quiet_billing(monkeypatch):
    monkeypatch.setenv("BILLING_MODE", "off")
    set_cache(None)
    yield
    set_cache(None)


@pytest.fixture
def default_config(monkeypatch):
    from core.config import OpenBoxConfig

    config = OpenBoxConfig.model_validate({
        "jwt_secret": "x" * 32,
        "video_generation": {"provider": "test", "models": [
            {"id": "wan3.0-video", "channel": "sd2", "resolutions": ["720p", "1080p"]},
            {"id": "MiniMax-H3", "channel": "sd2", "resolutions": ["768p"]},
        ]},
        "model_tiers": {"video": [
            {"tier": "high", "model": "wan3.0-video", "resolutions": ["1080p"]},
            {"tier": "medium", "model": "wan3.0-video"},
            {"tier": "low", "model": "MiniMax-H3"},
        ]},
    })
    monkeypatch.setattr("core.config.get_config", lambda: config)
    return config


async def test_create_and_get_session(default_config):
    uid, wid = await seed_scope()
    identity = key_identity(uid, wid)
    async with client(identity) as http:
        created = await http.post("/sessions", json={
            "title": "五一杭州自驾攻略口播", "quality": "high", "metadata": {"task_id": "t_456"},
        })
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["id"].startswith("ses_")
        assert body["status"] == "idle" and body["quality"] == "high"
        assert body["metadata"] == {"task_id": "t_456"} and body["credits_used"] == "0"
        assert created.headers["X-Request-Id"].startswith("req_")

        async with get_db_session() as db:
            row = await db.scalar(select(SessionRow).where(SessionRow.api_key_id == identity["api_key_id"]))
        assert row.quality == "high" and row.video_model == "wan3.0-video" and row.video_resolution == "1080p"
        assert row.workspace_id == wid and row.user_id == uid
        async with get_db_session() as db:
            db.add(UsageEvent(id=ascending("usage"), idempotency_key=uuid4().hex, workspace_id=wid,
                              user_id=uid, session_id=row.id, session_title="t", model_id="m", kind="chat",
                              tokens={}, total_tokens=0, credits=Decimal("12.5"), status="charged",
                              pricing={}, created_at=datetime.now(timezone.utc)))
            db.add(UsageEvent(id=ascending("usage"), idempotency_key=uuid4().hex, workspace_id=wid,
                              user_id=uid, session_id=row.id, session_title="t", model_id="m", kind="chat",
                              tokens={}, total_tokens=0, credits=Decimal("1"), status="unpriced",
                              pricing={}, created_at=datetime.now(timezone.utc)))
        fetched = await http.get(f"/sessions/{body['id']}")
        assert fetched.status_code == 200
        assert fetched.json()["credits_used"] == "12.5"

        default = await http.post("/sessions", json={})
        assert default.json()["quality"] == "medium"
        missing = await http.get("/sessions/ses_01J000000000000000000000AA")
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "NOT_FOUND"
        assert missing.json()["error"]["request_id"] == missing.headers["X-Request-Id"]


async def test_session_validation_errors_use_the_contract(default_config):
    uid, wid = await seed_scope()
    async with client(key_identity(uid, wid)) as http:
        low = await http.post("/sessions", json={"quality": "low"})
        assert low.status_code == 400 and low.json()["error"]["code"] == "INVALID_REQUEST"
        bogus = await http.post("/sessions", json={"quality": "ultra"})
        assert bogus.status_code == 400
        too_many = await http.post("/sessions", json={"metadata": {f"k{i}": "v" for i in range(17)}})
        assert too_many.status_code == 400
        assert too_many.json()["error"]["details"] == {"field": "metadata"}
        not_strings = await http.post("/sessions", json={"metadata": {"n": 1}})
        assert not_strings.status_code == 400


async def test_other_workspace_sessions_are_invisible(default_config):
    uid, wid = await seed_scope()
    other_uid, other_wid = await seed_scope()
    async with client(key_identity(uid, wid)) as http:
        created = (await http.post("/sessions", json={})).json()
    async with client(key_identity(other_uid, other_wid)) as http:
        assert (await http.get(f"/sessions/{created['id']}")).status_code == 404


async def test_scope_and_rate_limit_refusals(default_config):
    uid, wid = await seed_scope()
    async with client(key_identity(uid, wid, scopes=["sessions:read"])) as http:
        denied = await http.post("/sessions", json={})
        assert denied.status_code == 403
        assert denied.json()["error"] == {
            "code": "SCOPE_REQUIRED", "message": "This API key lacks the sessions:write scope",
            "request_id": denied.headers["X-Request-Id"], "details": {"scope": "sessions:write"},
        }
    set_cache(MemoryCache())
    async with client(key_identity(uid, wid, rate_limit="2/minute")) as http:
        first = await http.post("/sessions", json={})
        assert first.status_code == 201
        assert first.headers["X-RateLimit-Limit"] == "2" and first.headers["X-RateLimit-Remaining"] == "1"
        await http.post("/sessions", json={})
        third = await http.post("/sessions", json={})
        assert third.status_code == 429
        assert third.json()["error"]["code"] == "RATE_LIMITED"
        assert int(third.headers["Retry-After"]) >= 1
        assert third.headers["X-RateLimit-Remaining"] == "0"


async def _session(http) -> str:
    return (await http.post("/sessions", json={})).json()["id"]


async def test_send_message_accepts_through_the_inbox(default_config, monkeypatch):
    uid, wid = await seed_scope()
    identity = key_identity(uid, wid)
    calls = {}
    user_message_id = ascending("message")

    async def accept(**kwargs):
        calls["accept"] = kwargs
        return SimpleNamespace(id="inbox_1", state="accepted", created=True, message_id=None)

    async def wake(session_id, user_id):
        calls["wake"] = (session_id, user_id)
        return "run_1"

    async def get_item(item_id, *, user_id, session_id=None):
        return SimpleNamespace(id=item_id, state="claimed", message_id=user_message_id)

    monkeypatch.setattr("agent.inbox.accept_inbox_item", accept)
    monkeypatch.setattr("agent.inbox.wake_inbox_session", wake)
    monkeypatch.setattr("agent.inbox.get_inbox_item", get_item)

    async with client(identity) as http:
        session_id = await _session(http)
        sent = await http.post(f"/sessions/{session_id}/messages", json={
            "text": "帮我做一条 25 秒口播", "attachments": ["fil_01A"], "client_message_id": "t_456-1",
        })
        assert sent.status_code == 202, sent.text
        body = sent.json()
        assert body["session_id"] == session_id
        assert body["user_message_id"] == f"msg_{user_message_id.split('_', 1)[1]}"
        from api.v1.ids import assistant_turn_id
        assert body["assistant_message_id"] == assistant_turn_id(user_message_id)
    accepted = calls["accept"]
    assert accepted["delivery"] == "followup"
    assert accepted["attachments"] == ["asset_01A"]
    assert accepted["client_id"] == "t_456-1"
    assert accepted["video_model"] == "wan3.0-video" and accepted["video_resolution"] == "1080p"
    assert accepted["user_id"] == uid
    assert calls["wake"][1] == uid


async def test_send_message_refusals(default_config, monkeypatch):
    uid, wid = await seed_scope()
    identity = key_identity(uid, wid, policy={**keys.DEFAULT_POLICY, "max_concurrent_sessions": 1})
    async with client(identity) as http:
        session_id = await _session(http)
        busy_id = await _session(http)
        async with get_db_session() as db:
            from api.v1.ids import internal_id
            row = await db.get(SessionRow, internal_id(busy_id, "session"))
            row.status = "waiting_input"
        busy = await http.post(f"/sessions/{busy_id}/messages", json={"text": "hi"})
        assert busy.status_code == 409 and busy.json()["error"]["code"] == "SESSION_BUSY"

        # The other session is in progress under this key, so its cap of one is spent.
        capped = await http.post(f"/sessions/{session_id}/messages", json={"text": "hi"})
        assert capped.status_code == 429
        assert capped.json()["error"]["code"] == "CONCURRENT_LIMIT_EXCEEDED"

        async with get_db_session() as db:
            row = await db.get(SessionRow, internal_id(busy_id, "session"))
            row.status = "idle"

        from billing.service import BillingError

        async def broke(workspace_id):
            raise BillingError("INSUFFICIENT_CREDITS", "积分不足")

        monkeypatch.setattr("billing.service.precheck_balance", broke)
        poor = await http.post(f"/sessions/{session_id}/messages", json={"text": "hi"})
        assert poor.status_code == 402 and poor.json()["error"]["code"] == "INSUFFICIENT_CREDITS"
        monkeypatch.undo()
        monkeypatch.setenv("BILLING_MODE", "off")

        from agent.inbox import InboxIdempotencyConflict

        async def conflict(**kwargs):
            raise InboxIdempotencyConflict("inbox client id is already bound to different input")

        monkeypatch.setattr("agent.inbox.accept_inbox_item", conflict)
        dup = await http.post(f"/sessions/{session_id}/messages", json={"text": "hi", "client_message_id": "x"})
        assert dup.status_code == 409 and dup.json()["error"]["code"] == "DUPLICATE_CLIENT_MESSAGE_ID"

        huge = await http.post(f"/sessions/{session_id}/messages", json={"text": "字" * 8000})
        assert huge.status_code == 413 and huge.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"
        empty = await http.post(f"/sessions/{session_id}/messages", json={"text": ""})
        assert empty.status_code == 400
        reserved = await http.post(f"/sessions/{session_id}/messages", json={"text": "x", "client_message_id": "sjr:1"})
        assert reserved.status_code == 400


async def test_precheck_balance_only_bites_in_enforce_mode(monkeypatch):
    uid, wid = await seed_scope()
    from billing.service import BillingError, precheck_balance

    from billing.service import lock_balance, post_ledger

    monkeypatch.setenv("BILLING_MODE", "shadow")
    await precheck_balance(wid)
    # A fresh workspace is credited its free allowance first, like the meter does.
    monkeypatch.setenv("BILLING_MODE", "enforce")
    await precheck_balance(wid)
    async with get_db_session() as db:
        account = await lock_balance(db, wid)
        post_ledger(db, account, amount=-account.balance, kind="usage", reference_id="t", key=uuid4().hex)
    with pytest.raises(BillingError) as exc:
        await precheck_balance(wid)
    assert exc.value.code == "INSUFFICIENT_CREDITS"
    async with get_db_session() as db:
        account = await lock_balance(db, wid)
        post_ledger(db, account, amount=Decimal("5"), kind="grant", reference_id="t", key=uuid4().hex)
    await precheck_balance(wid)


async def test_poll_messages_returns_folded_turns(default_config, monkeypatch):
    uid, wid = await seed_scope()
    async with client(key_identity(uid, wid)) as http:
        session_id = await _session(http)
        from api.v1.ids import internal_id
        from session import session as session_mod
        internal = internal_id(session_id, "session")
        user = await session_mod.create_user_message(session_id=internal, text="hello", user_id=uid)
        assistant = await session_mod.create_assistant_message(
            session_id=internal, parent_id=user.id, model_id="m", agent="build", user_id=uid,
        )
        from models.message import TextPart
        await session_mod.save_part(TextPart(text="hi there", session_id=internal, message_id=assistant.id),
                                    is_new=True, user_id=uid)
        assistant.finish = "stop"
        await session_mod.update_message_info(assistant, user_id=uid)

        page = await http.get(f"/sessions/{session_id}/messages")
        assert page.status_code == 200, page.text
        data = page.json()["data"]
        assert [m["role"] for m in data] == ["user", "assistant"]
        assert data[0]["parts"] == [{"type": "text", "id": data[0]["parts"][0]["id"], "text": "hello"}]
        assert data[1]["finish"] == "stop"
        assert data[1]["parts"][0]["text"] == "hi there"
        assert page.json()["has_more"] is False

        after = await http.get(f"/sessions/{session_id}/messages", params={"after": data[0]["id"], "limit": 1})
        assert [m["id"] for m in after.json()["data"]] == [data[0]["id"]]
        assert after.json()["has_more"] is True
        by_turn = await http.get(f"/sessions/{session_id}/messages", params={"after": data[1]["id"]})
        assert [m["id"] for m in by_turn.json()["data"]] == [data[1]["id"]]
        unknown = await http.get(f"/sessions/{session_id}/messages", params={"after": "msg_nope"})
        assert unknown.status_code == 404


async def test_question_endpoints_map_outcomes(default_config, monkeypatch):
    uid, wid = await seed_scope()
    from question import question as q_mod

    async with client(key_identity(uid, wid)) as http:
        session_id = await _session(http)
        from api.v1.ids import internal_id
        internal = internal_id(session_id, "session")
        seen = {}

        async def get_request(request_id, user_id):
            seen["lookup"] = (request_id, user_id)
            if request_id == "MISSING":
                raise KeyError("Question not found")
            return SimpleNamespace(
                id=request_id, status="answered" if request_id == "DONE" else "pending",
                session_id="session_other" if request_id == "ELSEWHERE" else internal,
            )

        async def reply(request_id, answers, user_id="default"):
            seen["reply"] = (request_id, answers, user_id)
            if request_id == "GONE":
                raise q_mod.QuestionGone("expired")
            if request_id == "BAD":
                raise ValueError("Choose one of the offered options")
            return {"ok": True}

        async def reject(request_id, user_id="default"):
            seen["reject"] = (request_id, user_id)
            return {"ok": True}

        monkeypatch.setattr(q_mod, "get_request", get_request)
        monkeypatch.setattr(q_mod, "reply", reply)
        monkeypatch.setattr(q_mod, "reject", reject)

        ok = await http.post(f"/sessions/{session_id}/questions/qst_Q1", json={"answers": [["可以"]]})
        assert ok.status_code == 200 and ok.json() == {"ok": True}
        assert seen["lookup"] == ("Q1", uid) and seen["reply"] == ("Q1", [["可以"]], uid)
        gone = await http.post(f"/sessions/{session_id}/questions/qst_GONE", json={"answers": [["x"]]})
        assert gone.status_code == 409 and gone.json()["error"]["code"] == "INTERACTION_RESOLVED"
        bad = await http.post(f"/sessions/{session_id}/questions/qst_BAD", json={"answers": [["x"]]})
        assert bad.status_code == 400 and bad.json()["error"]["message"] == "Choose one of the offered options"
        missing = await http.post(f"/sessions/{session_id}/questions/qst_MISSING", json={"answers": [["x"]]})
        assert missing.status_code == 404
        elsewhere = await http.post(f"/sessions/{session_id}/questions/qst_ELSEWHERE/reject")
        assert elsewhere.status_code == 404
        rejected = await http.post(f"/sessions/{session_id}/questions/qst_Q2/reject")
        assert rejected.status_code == 200 and seen["reject"] == ("Q2", uid)
        # A repeat of the same answer is a 409 too: the contract says refresh the card.
        seen.pop("reply", None)
        repeat = await http.post(f"/sessions/{session_id}/questions/qst_DONE", json={"answers": [["可以"]]})
        assert repeat.status_code == 409 and repeat.json()["error"]["code"] == "INTERACTION_RESOLVED"
        assert "reply" not in seen


async def test_abort_reports_whether_anything_stopped(default_config, monkeypatch):
    uid, wid = await seed_scope()
    async with client(key_identity(uid, wid)) as http:
        session_id = await _session(http)
        idle = await http.post(f"/sessions/{session_id}/abort")
        assert idle.status_code == 200 and idle.json() == {"ok": True, "aborted": False}

        async def canceled(**kwargs):
            return ["inbox_1"]

        monkeypatch.setattr("agent.inbox.cancel_inbox_items", canceled)
        queued = await http.post(f"/sessions/{session_id}/abort")
        assert queued.json() == {"ok": True, "aborted": True}


class FakeOss:
    def __init__(self):
        self.objects = {}

    async def put_object_file(self, key, path, *, content_type, timeout=120, **_):
        with open(path, "rb") as handle:
            self.objects[key] = (handle.read(), content_type)
        return "etag"

    def presign_get(self, key, expires_sec=3600, download_name=None, **_):
        return f"https://oss.test/{key}?ttl={expires_sec}&name={download_name}"


async def test_file_upload_and_content_redirect(default_config, monkeypatch):
    uid, wid = await seed_scope()
    oss = FakeOss()
    monkeypatch.setattr(files_api, "_oss_or_503", lambda: oss)
    async with client(key_identity(uid, wid)) as http:
        uploaded = await http.post("/files", files={"file": ("road.MOV", b"\x00" * 1024, "application/octet-stream")})
        assert uploaded.status_code == 201, uploaded.text
        body = uploaded.json()
        assert body["id"].startswith("fil_") and body["filename"] == "road.MOV"
        assert body["mime_type"] == "video/quicktime" and body["size"] == 1024 and body["duration_s"] is None
        key = next(iter(oss.objects))
        assert key.startswith(f"assets/{uid}/asset_") and oss.objects[key][1] == "video/quicktime"
        async with get_db_session() as db:
            row = await db.scalar(select(FileAsset).where(FileAsset.oss_key == key))
        assert row.status == "ready" and row.workspace_id == wid and row.size == 1024

        content = await http.get(f"/files/{body['id']}/content")
        assert content.status_code == 302
        assert content.headers["location"] == f"https://oss.test/{key}?ttl=86400&name=road.MOV"

        odd = await http.post("/files", files={"file": ("notes.txt", b"hi", "text/plain")})
        assert odd.status_code == 415 and odd.json()["error"]["code"] == "UNSUPPORTED_MEDIA_TYPE"
        by_mime = await http.post("/files", files={"file": ("blob", b"hi", "image/png")})
        assert by_mime.status_code == 201 and by_mime.json()["filename"] == "blob.png"
        empty = await http.post("/files", files={"file": ("a.png", b"", "image/png")})
        assert empty.status_code == 400
        monkeypatch.setattr(files_api, "MAX_BYTES", 10)
        big = await http.post("/files", files={"file": ("a.png", b"x" * 11, "image/png")})
        assert big.status_code == 413 and big.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"
        gone = await http.get("/files/fil_nope/content")
        assert gone.status_code == 404

    other_uid, other_wid = await seed_scope()
    async with client(key_identity(other_uid, other_wid)) as http:
        assert (await http.get(f"/files/{body['id']}/content")).status_code == 404


async def test_jwt_callers_are_not_rate_limited_and_keep_workspace_header(default_config):
    uid, wid = await seed_scope()
    set_cache(MemoryCache())
    identity = {"user_id": uid, "role": "user"}
    app = create_v1_app()
    app.dependency_overrides[get_current_user] = lambda: identity
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        created = await http.post("/sessions", json={}, headers={"X-Workspace-Id": wid})
        assert created.status_code == 201, created.text
        assert "X-RateLimit-Limit" not in created.headers
        async with get_db_session() as db:
            from api.v1.ids import internal_id
            row = await db.get(SessionRow, internal_id(created.json()["id"], "session"))
        assert row.api_key_id is None and row.workspace_id == wid


async def test_v1_answers_cors_for_any_origin(default_config):
    uid, wid = await seed_scope()
    async with client(key_identity(uid, wid)) as http:
        preflight = await http.options("/sessions", headers={
            "Origin": "https://partner.example", "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        })
        assert preflight.status_code == 200
        assert preflight.headers["access-control-allow-origin"] == "*"
        assert "authorization" in preflight.headers["access-control-allow-headers"].lower()
        created = await http.post("/sessions", json={}, headers={"Origin": "https://partner.example"})
        assert created.status_code == 201
        assert created.headers["access-control-allow-origin"] == "*"
        assert "X-Request-Id" in created.headers["access-control-expose-headers"]


async def test_parent_cors_hands_v1_preflight_to_the_sub_app(default_config):
    """Mounted like main.py: the web app's own CORS policy must not answer /v1 preflights."""
    from fastapi import FastAPI

    from api.v1.app import CORSMiddlewareExemptingV1

    uid, wid = await seed_scope()
    parent = FastAPI()
    parent.add_middleware(CORSMiddlewareExemptingV1, allow_origins=["https://app.example"],
                          allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
    v1 = create_v1_app()
    v1.dependency_overrides[get_current_user] = lambda: key_identity(uid, wid)
    parent.mount("/v1", v1)

    @parent.get("/api/ping")
    async def ping():
        return {"ok": True}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=parent), base_url="http://test") as http:
        partner = await http.options("/v1/sessions", headers={
            "Origin": "https://partner.example", "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        })
        assert partner.status_code == 200
        assert partner.headers["access-control-allow-origin"] == "*"
        # The web app keeps its own policy for everything else.
        blocked = await http.options("/api/ping", headers={
            "Origin": "https://partner.example", "Access-Control-Request-Method": "GET",
        })
        assert blocked.status_code == 400
        allowed = await http.options("/api/ping", headers={
            "Origin": "https://app.example", "Access-Control-Request-Method": "GET",
        })
        assert allowed.status_code == 200
        assert allowed.headers["access-control-allow-origin"] == "https://app.example"


async def test_replay_of_an_accepted_prompt_bypasses_the_busy_gate(default_config, monkeypatch):
    uid, wid = await seed_scope()
    user_message_id = ascending("message")
    accepted = SimpleNamespace(id="inbox_1", state="claimed", message_id=user_message_id,
                               prompt="hi", attachments=["asset_1"])

    async def accept(**kwargs):
        raise AssertionError("a replay must never enter the acceptance transaction")

    monkeypatch.setattr("agent.inbox.accept_inbox_item", accept)

    async def lookup(session_id, user_id, client_message_id):
        return accepted if client_message_id == "t-1" else None

    monkeypatch.setattr("api.v1.messages._accepted_item", lookup)
    async with client(key_identity(uid, wid)) as http:
        session_id = await _session(http)
        async with get_db_session() as db:
            from api.v1.ids import internal_id
            (await db.get(SessionRow, internal_id(session_id, "session"))).status = "busy"
        fresh = await http.post(f"/sessions/{session_id}/messages", json={"text": "hi", "client_message_id": "t-2"})
        assert fresh.status_code == 409 and fresh.json()["error"]["code"] == "SESSION_BUSY"
        replay = await http.post(f"/sessions/{session_id}/messages",
                                 json={"text": "hi", "attachments": ["fil_1"], "client_message_id": "t-1"})
        assert replay.status_code == 202
        assert replay.json()["user_message_id"] == f"msg_{user_message_id.split('_', 1)[1]}"
        conflict = await http.post(f"/sessions/{session_id}/messages",
                                   json={"text": "different", "attachments": ["fil_1"], "client_message_id": "t-1"})
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "DUPLICATE_CLIENT_MESSAGE_ID"


async def test_pending_video_job_keeps_session_busy_and_turn_open(default_config):
    from datetime import datetime, timezone

    from db.models.video_job import VideoJob

    uid, wid = await seed_scope()
    async with client(key_identity(uid, wid)) as http:
        session_id = await _session(http)
        from api.v1.ids import internal_id
        from session import session as session_mod
        internal = internal_id(session_id, "session")
        user = await session_mod.create_user_message(session_id=internal, text="make it", user_id=uid)
        assistant = await session_mod.create_assistant_message(
            session_id=internal, parent_id=user.id, model_id="m", agent="build", user_id=uid,
        )
        assistant.finish = "stop"
        await session_mod.update_message_info(assistant, user_id=uid)
        now = datetime.now(timezone.utc)
        async with get_db_session() as db:
            db.add(VideoJob(id="video_1", user_id=uid, session_id=internal, kind="segment",
                            idempotency_key=f"k-{uuid4().hex}", status="in_progress", request_data={},
                            result_data={}, created_at=now, updated_at=now))
        page = (await http.get(f"/sessions/{session_id}/messages")).json()["data"]
        assert page[1]["finish"] is None
        assert (await http.get(f"/sessions/{session_id}")).json()["status"] == "busy"
        busy = await http.post(f"/sessions/{session_id}/messages", json={"text": "again"})
        assert busy.status_code == 409 and busy.json()["error"]["code"] == "SESSION_BUSY"
        async with get_db_session() as db:
            (await db.get(VideoJob, "video_1")).status = "completed"
        page = (await http.get(f"/sessions/{session_id}/messages")).json()["data"]
        assert page[1]["finish"] == "stop"
        assert (await http.get(f"/sessions/{session_id}")).json()["status"] == "idle"


async def test_unknown_endpoint_and_preflight_follow_the_contract(default_config):
    uid, wid = await seed_scope()
    async with client(key_identity(uid, wid)) as http:
        missing = await http.get("/not-a-route")
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "NOT_FOUND"
        assert missing.json()["error"]["request_id"] == missing.headers["X-Request-Id"]
        # A wrong method on a known path is an unknown endpoint too (the
        # catch-all owns every method), never a bare Starlette body.
        wrong = await http.delete("/files")
        assert wrong.status_code == 404 and wrong.json()["error"]["code"] == "NOT_FOUND"
        preflight = await http.options("/sessions", headers={
            "Origin": "https://partner.example", "Access-Control-Request-Method": "POST",
        })
        assert preflight.status_code == 200 and preflight.headers["X-Request-Id"].startswith("req_")


async def test_list_sessions_pages_newest_first_within_the_workspace(default_config):
    uid, wid = await seed_scope()
    other_uid, other_wid = await seed_scope()
    async with client(key_identity(other_uid, other_wid)) as http:
        await http.post("/sessions", json={"title": "not mine"})
    async with client(key_identity(uid, wid)) as http:
        ids = [(await http.post("/sessions", json={"title": f"s{i}"})).json()["id"] for i in range(3)]
        first = await http.get("/sessions", params={"limit": 2})
        assert first.status_code == 200, first.text
        page = first.json()
        assert [s["id"] for s in page["data"]] == [ids[2], ids[1]]
        assert page["has_more"] is True and page["next_cursor"]
        assert page["data"][0]["title"] == "s2" and page["data"][0]["credits_used"] == "0"
        second = await http.get("/sessions", params={"limit": 2, "cursor": page["next_cursor"]})
        assert [s["id"] for s in second.json()["data"]] == [ids[0]]
        assert second.json()["has_more"] is False and second.json()["next_cursor"] is None
        assert "not mine" not in [s["title"] for s in page["data"] + second.json()["data"]]
        bad = await http.get("/sessions", params={"cursor": "nope"})
        assert bad.status_code == 400
