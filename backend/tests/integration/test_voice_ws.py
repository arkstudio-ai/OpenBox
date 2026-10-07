"""/ws/assistant/voice end to end with a scripted provider: handshake, refusals, a turn, hang-up."""
import json
import os
import secrets

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from starlette.testclient import TestClient

from tests.support.voice_fakes import ASK_MARKER, ScriptedProvider

os.environ["JWT_SECRET"] = "test-voice-secret-32bytes-long!!"
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
os.environ["REDIS_URL"] = "redis://localhost:6379/0"

FRAME = b"\x00\x00" * 1600
_cache = None


@pytest.fixture(scope="module")
async def app(tmp_path_factory):
    global _cache
    from cache.memory_cache import MemoryCache
    from core.config import reload_config
    from db.base import Base, close_engine, init_engine
    _cache = MemoryCache()
    config = reload_config()
    engine = init_engine(f"sqlite+aiosqlite:///{tmp_path_factory.mktemp('voice-db') / 'test.db'}")
    import db.models  # noqa: F401
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    from auth import setup_auth
    setup_auth(config, _cache)
    from main import create_app
    yield create_app()
    await close_engine()


@pytest.fixture
def providers(app, monkeypatch):
    """Voice switched on with a scripted provider; Inbox wakes are not dispatched."""
    from api import voice as voice_socket
    from core.config import VoiceConfig, get_config
    _cache._store.clear()
    monkeypatch.setattr(get_config(), "voice", VoiceConfig(enabled=True, api_key="test-only"))
    monkeypatch.setattr("agent.inbox.schedule_inbox_wake", lambda *_: None)
    made = []

    def factory(config, *, debug=False):
        made.append(ScriptedProvider(config, debug=debug, auto_reply=True, fail_open=getattr(factory, "fail", False)))
        return made[-1]
    monkeypatch.setattr(voice_socket, "provider_factory", factory)
    factory.made = made
    return factory


@pytest.fixture
async def http(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


async def account(http, *, ensure=True):
    response = await http.post("/api/auth/register", json={"username": f"voice_{secrets.token_hex(4)}",
                                                           "password": "password123"})
    headers = {"Authorization": f"Bearer {response.json()['access_token']}"}
    if ensure:
        ensured = await http.post("/api/assistant/ensure", json={"model": "test/model"}, headers=headers)
        assert ensured.status_code == 200
    return headers


async def ticket(http, headers, audience="voice"):
    body = {"json": {"audience": audience}} if audience else {}
    response = await http.post("/api/auth/ticket", headers=headers, **body)
    assert response.status_code == 200, response.text
    return response.json()["ticket"]


def read_until(socket, predicate):
    """Messages up to and including the first JSON event matching ``predicate`` (or the close)."""
    seen = []
    while True:
        message = socket.receive()
        if message["type"] == "websocket.close":
            seen.append({"type": "<close>", "code": message["code"]})
            return seen
        seen.append(message["bytes"] if message.get("bytes") is not None else json.loads(message["text"]))
        if isinstance(seen[-1], dict) and predicate(seen[-1]):
            return seen


def kinds(items):
    return [item["type"] if isinstance(item, dict) else "<audio>" for item in items]


async def test_refusals_carry_the_contract_close_codes(http, providers, app, monkeypatch):
    client = TestClient(app)
    headers = await account(http)

    def close_code(url):
        with client.websocket_connect(url) as socket:
            return socket.receive()["code"]
    assert close_code(f"/ws/assistant/voice?ticket={await ticket(http, headers, audience=None)}") == 4001
    assert close_code("/ws/assistant/voice?ticket=never-issued") == 4001
    assert close_code(f"/ws/assistant/voice?ticket={await ticket(http, await account(http, ensure=False))}") == 4404
    from core.config import VoiceConfig, get_config
    monkeypatch.setattr(get_config(), "voice", VoiceConfig(enabled=False, api_key="test-only"))
    assert close_code(f"/ws/assistant/voice?ticket={await ticket(http, headers)}") == 4503
    monkeypatch.setattr(get_config(), "voice", VoiceConfig(enabled=True, api_key="", daily_seconds=60))
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    assert close_code(f"/ws/assistant/voice?ticket={await ticket(http, headers)}") == 4503
    assert not providers.made


async def test_a_call_greets_answers_ping_and_hangs_up_with_its_cost(http, providers, app):
    from db.base import get_db_session
    from db.models.voice import VoiceCall
    client = TestClient(app)
    headers = await account(http)
    with client.websocket_connect(f"/ws/assistant/voice?ticket={await ticket(http, headers)}") as socket:
        ready = socket.receive_json()
        assert ready["type"] == "ready" and ready["model"] == "qwen3.8-omni-flash-realtime"
        assert (ready["input_sample_rate"], ready["output_sample_rate"], ready["max_seconds"]) == (16000, 24000, 1800)
        assert ready["price_date"] == "2026-10-07"
        greeting = read_until(socket, lambda item: item["type"] == "phase" and item["value"] == "listening")
        assert kinds(greeting) == ["phase", "phrase", "<audio>", "cost", "phase"]
        assert greeting[0]["value"] == "greeting" and greeting[1] == {"type": "phrase", "key": "greeting"}
        assert "嗨，我在，你说。" in providers.made[0].commands("create")[0][1]
        assert "你是 OpenBox 个人助理的语音前台" in providers.made[0].instructions
        socket.send_bytes(FRAME)
        socket.send_json({"type": "ping"})
        assert read_until(socket, lambda item: item["type"] == "heartbeat")[-1]["elapsed_seconds"] >= 0
        socket.send_json({"type": "stop"})
        ending = read_until(socket, lambda item: False)
    assert kinds(ending) == ["cost", "ended", "<close>"] and ending[-1]["code"] == 1000
    ended = ending[1]
    assert (ended["reason"], ended["pending_turns"]) == ("hangup", 0) and ended["cost"]["final"]
    assert providers.made[0].commands("audio") and providers.made[0].closed
    async with get_db_session() as db:
        row = await db.scalar(select(VoiceCall).where(VoiceCall.id == ready["call_id"]))
    assert (row.status, row.end_reason, row.client) == ("ended", "hangup", "web")
    assert row.usage["input_text"] == 1180 and row.estimated_yuan == ended["cost"]["total_yuan"]


async def test_a_turn_reaches_the_main_session_and_hang_up_reports_it_pending(http, providers, app):
    from db.base import get_db_session
    from db.models.agent_inbox import AgentInboxItem
    from db.models.voice import VoiceTurn
    client = TestClient(app)
    headers = await account(http)
    with client.websocket_connect(f"/ws/assistant/voice?ticket={await ticket(http, headers)}") as socket:
        read_until(socket, lambda item: item["type"] == "phase" and item["value"] == "listening")
        socket.send_bytes(ASK_MARKER + FRAME[len(ASK_MARKER):])
        accepted = read_until(socket, lambda item: item["type"] == "turn")[-1]
        assert accepted["state"] == "accepted" and accepted["inbox_id"]
        socket.send_json({"type": "stop"})
        ended = read_until(socket, lambda item: item["type"] == "ended")[-1]
    assert ended["pending_turns"] == 1
    async with get_db_session() as db:
        item = await db.get(AgentInboxItem, accepted["inbox_id"])
        turn = await db.scalar(select(VoiceTurn).where(VoiceTurn.id == accepted["turn_id"]))
    assert item.prompt == "帮我看看贪吃蛇进展" and item.origin_ref["entrypoint"] == "assistant_voice"
    assert (turn.outcome, turn.inbox_id, turn.provider_call_id) == ("late", item.id, "call-2")


async def test_one_call_per_user_bad_frames_quota_and_provider_failure(http, providers, app, monkeypatch):
    from db.base import get_db_session
    from db.models.voice import VoiceCall
    client = TestClient(app)
    headers = await account(http)
    with client.websocket_connect(f"/ws/assistant/voice?ticket={await ticket(http, headers)}") as first:
        ready = first.receive_json()
        assert ready["type"] == "ready"
        with client.websocket_connect(f"/ws/assistant/voice?ticket={await ticket(http, headers)}") as second:
            assert second.receive()["code"] == 4009
        first.send_bytes(b"\x00" * 3201)  # odd length
        ending = read_until(first, lambda item: False)
    assert [item for item in ending if isinstance(item, dict) and item["type"] == "error"] == [
        {"type": "error", "code": "bad_frame", "message": "音频数据不正确，通话已结束。"}]
    assert kinds(ending)[-3:] == ["cost", "ended", "<close>"] and ending[-1]["code"] == 4400
    providers.fail = True
    with client.websocket_connect(f"/ws/assistant/voice?ticket={await ticket(http, headers)}") as socket:
        failed = read_until(socket, lambda item: False)
    assert failed == [{"type": "error", "code": "provider_unavailable", "message": "没能接通，请稍后再试。"},
                      {"type": "<close>", "code": 1011}]
    async with get_db_session() as db:
        user_id = (await db.get(VoiceCall, ready["call_id"])).user_id
        statuses = (await db.scalars(select(VoiceCall.status).where(VoiceCall.user_id == user_id)
                                     .order_by(VoiceCall.started_at))).all()
        assert statuses == ["failed", "failed"]  # the bad frame, then the provider that never answered
        await db.execute(VoiceCall.__table__.update().where(VoiceCall.id == ready["call_id"]).values(
            duration_seconds=3600))
    with client.websocket_connect(f"/ws/assistant/voice?ticket={await ticket(http, headers)}") as socket:
        assert socket.receive()["code"] == 4029
